import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from niftypaper.contract_specs import ContractSpecError, load_contract_specs
from tests import ROOT

GOOD = {"root_symbol": "TEST", "instrument_type": "index_option", "lot_size": 10, "tick_size": "0.05", "valid_from": "2026-01-01",
        "verification_status": "VERIFIED", "note": "synthetic"}


def write(tmp, specs, schema_version=1):
    path = Path(tmp) / "specs.json"
    path.write_text(json.dumps({"schema_version": schema_version, "specs": specs}), encoding="utf-8")
    return path


class TestContractSpecs(unittest.TestCase):
    def test_shipped_file_loads_and_is_marked_unverified(self):
        specs = load_contract_specs(ROOT / "config" / "contract_specs.json")
        nifty = specs["NIFTY"]
        self.assertEqual((nifty.lot_size, nifty.tick_size), (65, Decimal("0.05")))
        self.assertFalse(nifty.is_verified)                      # must not pretend to be official
        self.assertIsNone(nifty.valid_from)
        policy = nifty.eligibility_policy(max_age=timedelta(seconds=5), max_future_skew=timedelta(seconds=1))
        self.assertEqual(policy.tick_size, Decimal("0.05"))

    def test_valid_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = load_contract_specs(write(tmp, [GOOD]))["TEST"]
            self.assertTrue(spec.is_verified)
            self.assertEqual(spec.valid_from.isoformat(), "2026-01-01")

    def test_rejections(self):
        bad_cases = {
            "zero lot": {**GOOD, "lot_size": 0}, "negative lot": {**GOOD, "lot_size": -65}, "float lot": {**GOOD, "lot_size": 65.0},
            "bool lot": {**GOOD, "lot_size": True}, "string lot": {**GOOD, "lot_size": "65"},
            "zero tick": {**GOOD, "tick_size": "0"}, "negative tick": {**GOOD, "tick_size": "-0.05"}, "numeric tick": {**GOOD, "tick_size": 0.05},
            "nan tick": {**GOOD, "tick_size": "NaN"}, "text tick": {**GOOD, "tick_size": "five paise"},
            "bad status": {**GOOD, "verification_status": "PROBABLY_FINE"}, "bad date": {**GOOD, "valid_from": "05/01/2026"},
            "blank symbol": {**GOOD, "root_symbol": " "}, "extra key": {**GOOD, "leverage": 5},
            "missing key": {k: v for k, v in GOOD.items() if k != "note"}, "not an object": "NIFTY",
        }
        for name, case in bad_cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ContractSpecError):
                    load_contract_specs(write(tmp, [case]))

    def test_duplicates_empty_wrong_schema_and_unreadable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ContractSpecError):
                load_contract_specs(write(tmp, [GOOD, GOOD]))
            with self.assertRaises(ContractSpecError):
                load_contract_specs(write(tmp, []))
            with self.assertRaises(ContractSpecError):
                load_contract_specs(write(tmp, [GOOD], schema_version=2))
            with self.assertRaises(ContractSpecError):
                load_contract_specs(Path(tmp) / "missing.json")
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ContractSpecError):
                load_contract_specs(bad)


if __name__ == "__main__":
    unittest.main()
