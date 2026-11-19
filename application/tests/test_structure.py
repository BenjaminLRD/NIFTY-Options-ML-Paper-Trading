"""Structural tests: the scaffold contains no network code, no order-placement code and no third-party dependencies.

These are static checks of THIS source tree. They show the scaffold as written has none of these things; they do not make the
system secure against a later change that adds them (the safety scan and review are the guards for that), see docs/SAFETY.md.
"""
import ast
import re
import tomllib
import unittest
from pathlib import Path

from tests import ROOT, SRC

NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "websocket", "websockets", "ftplib", "smtplib",
                   "telnetlib", "xmlrpc", "imaplib", "poplib", "asyncio", "selectors", "multiprocessing", "webbrowser"}
BROKER_NAME = re.compile(r"(?i)(broker|kite|upstox|angel|fyers|dhan|zerodha|groww|ib[k]r|alpaca)")
# V6-0.5: read-only data adapters under providers/ may carry their provider's name in identifiers; every other broker name,
# and every order-related name, stays banned there too.
BROKER_NAME_IN_PROVIDERS = re.compile(r"(?i)(broker|kite|upstox|angel|dhan|zerodha|groww|ib[k]r|alpaca)")
NETWORK_ALLOWANCES = {"niftypaper/dashboard/server.py": {"http"}, "niftypaper/providers/fyers_http.py": {"urllib"}}
ORDER_NAME = re.compile(r"(?i)(place|modify|cancel|submit|send|create|new)_?(\w*_)?order|order_?(place|submit|send)")


def source_files():
    return sorted(SRC.rglob("*.py"))


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


class TestStructure(unittest.TestCase):
    def test_source_tree_is_not_empty(self):
        names = {p.name for p in source_files()}
        self.assertTrue({"eligibility.py", "quotes.py", "contract_specs.py", "scan.py"} <= names)

    def test_no_network_modules_are_imported(self):
        # The only allowances: the local dashboard server may import `http` (loopback, GET/HEAD only, V6-0.2) and the read-only
        # market-data client may import `urllib` (two allow-listed hosts, GET only, V6-0.5). Everything else stays banned.
        for path in source_files():
            allowed = NETWORK_ALLOWANCES.get(path.relative_to(SRC).as_posix(), set())
            with self.subTest(file=path.name):
                self.assertEqual((imported_roots(parse(path)) & NETWORK_MODULES) - allowed, set())

    def test_network_modules_are_imported_only_where_allowed(self):
        for module in ("http", "urllib"):
            users = {p.relative_to(SRC).as_posix() for p in source_files() if module in imported_roots(parse(p))}
            expected = {name for name, allowed in NETWORK_ALLOWANCES.items() if module in allowed}
            self.assertEqual(users, expected, module)

    def test_subprocess_is_used_only_by_the_local_git_listing_in_the_scanner(self):
        users = {p.relative_to(SRC).as_posix() for p in source_files() if "subprocess" in imported_roots(parse(p))}
        self.assertEqual(users, {"niftypaper/safety/scan.py"})

    def test_only_standard_library_modules_are_imported(self):
        import sys
        stdlib = set(sys.stdlib_module_names)
        for path in source_files():
            for root in imported_roots(parse(path)):
                with self.subTest(file=path.name, module=root):
                    self.assertTrue(root in stdlib or root in {"niftypaper", "tests"}, f"third-party import {root}")

    def test_pyproject_declares_no_dependencies(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["dependencies"], [])
        self.assertNotIn("optional-dependencies", data["project"])

    def test_no_function_class_or_attribute_names_for_orders_or_brokers(self):
        for path in source_files():
            if path.name == "scan.py":
                continue            # the scanner names its rule categories; its own text is checked by the repository scan test
            for node in ast.walk(parse(path)):
                names = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append(node.name)
                elif isinstance(node, ast.Attribute):
                    names.append(node.attr)
                elif isinstance(node, ast.Name):
                    names.append(node.id)
                for name in names:
                    with self.subTest(file=path.name, name=name):
                        self.assertIsNone(ORDER_NAME.search(name))
                        pattern = BROKER_NAME_IN_PROVIDERS if "/providers/" in path.as_posix() or path.name == "assembly.py" else BROKER_NAME
                        self.assertIsNone(pattern.search(name))

    def test_safety_does_not_depend_on_a_paper_trading_flag(self):
        for path in source_files():
            self.assertNotIn("paper_trading", path.read_text(encoding="utf-8").lower())

    def test_refusals_are_not_implemented_as_warnings(self):
        for path in source_files():
            tree = parse(path)
            self.assertNotIn("warnings", imported_roots(tree))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotEqual(node.func.attr, "warn")

    def test_eligibility_has_no_alternative_price_path(self):
        """The only public price accessor for fills takes an ExecutableQuote; nothing reads settlement/last-trade values."""
        import niftypaper.eligibility as eligibility
        text = Path(eligibility.__file__).read_text(encoding="utf-8")
        self.assertEqual(eligibility.FILLABLE_TYPES, frozenset({eligibility.QuoteType.LIVE_QUOTE}))
        for forbidden in ("settle", "last_trade", "last_price", "close"):
            self.assertNotIn(f".{forbidden}", text)
        public_callables = [n for n in eligibility.__all__ if callable(getattr(eligibility, n)) and not isinstance(getattr(eligibility, n), type)]
        self.assertEqual(sorted(public_callables), ["check_fill_eligibility", "fill_price", "require_fillable"])


if __name__ == "__main__":
    unittest.main()
