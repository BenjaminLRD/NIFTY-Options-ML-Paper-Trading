"""Tests for Phase 1 of historical data ingestion: the read-only inventory tool, the data-source registry and the canonical schema document.
Everything uses small made-up files; no NSE file is read, downloaded or stored by these tests."""
import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from niftypaper.safety.scan import scan_text
from tests import ROOT

spec = importlib.util.spec_from_file_location("inventory_bhavcopy", ROOT / "tools" / "inventory_bhavcopy.py")
inv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inv)

HEADER = list(inv.UDIFF_COLUMNS) + ["Rsvd1", "Rsvd2"]


def row(date, symbol="NIFTY", kind="IDO", expiry="2026-09-29", strike="25000.00", opt="CE", token="1001", vol="10", txns="4", close="100.00", last="100.00", lot="65", **over):
    values = {c: "" for c in HEADER}
    values.update(TradDt=date, BizDt=date, Sgmt="FO", Src="NSE", FinInstrmTp=kind, FinInstrmId=token, TckrSymb=symbol, XpryDt=expiry, FininstrmActlXpryDt=expiry,
              StrkPric=strike, OptnTp=opt, FinInstrmNm=f"{symbol}X{strike}{opt}", OpnPric="95.00", HghPric="110.00", LwPric="90.00", ClsPric=close, LastPric=last,
              PrvsClsgPric="98.00", UndrlygPric="24900.00", SttlmPric=close, OpnIntrst="500", ChngInOpnIntrst="5", TtlTradgVol=vol, TtlTrfVal="123456.00",
              TtlNbOfTxsExctd=txns, SsnId="F1", NewBrdLotQty=lot)
    values.update(over)
    return values


def udiff_text(rows, header=None):
    header = header or HEADER
    out = io.StringIO()
    out.write(",".join(header) + "\n")
    for r in rows:
        out.write(",".join(str(r.get(c, "")) for c in header) + "\n")
    return out.getvalue()


def name_for(date):
    return f"BhavCopy_NSE_FO_0_0_0_{date.replace('-', '')}_F_0000.csv"


def write_csv(folder, date, rows, sub="", header=None):
    path = Path(folder) / sub / name_for(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(udiff_text(rows, header).encode("utf-8"))        # bytes, so Windows does not turn the line endings into CRLF
    return path


def write_zip(folder, date, rows, sub=""):
    path = Path(folder) / sub / (name_for(date) + ".zip")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name_for(date), udiff_text(rows))
    return path


def tree_state(folder):
    """Everything about a folder that a write, rename or delete would change."""
    state = {}
    for path in sorted(Path(folder).rglob("*")):
        if path.is_file():
            stat = path.stat()
            state[path.relative_to(folder).as_posix()] = (stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return state


class TestInventoryTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "research"
        self.root.mkdir()

    def build(self):
        return inv.build_inventory(self.root)

    def test_only_nifty_index_option_rows_are_counted_and_the_facts_are_reported(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01"), row("2026-06-01", opt="PE", token="1002", vol="0", txns="0", last="0.00", close="50.00"),
                                            row("2026-06-01", symbol="ABC", kind="STO", token="9"), row("2026-06-01", symbol="BANKNIFTY", token="8"),
                                            row("2026-06-01", kind="IDF", opt="XX", token="7")])
        report = self.build()
        f, = report["udiff"]["files"]
        self.assertEqual((f["rows"], f["nifty_option_rows"], f["nifty_ce"], f["nifty_pe"]), (5, 2, 1, 1))
        self.assertEqual((f["instrument_types"], f["nifty_lot_sizes"], f["nifty_session_ids"], f["nifty_expiries"]), ({"IDO": 3, "STO": 1, "IDF": 1}, ["65"], ["F1"], ["2026-09-29"]))
        self.assertEqual((f["nifty_rows_with"]["traded_volume"], f["nifty_rows_with"]["close_without_last_trade"], f["nifty_rows_with"]["close_gt0"]), (1, 1, 2))
        self.assertTrue(f["has_expected_columns"])
        self.assertEqual(f["quote_like_columns"], [])
        self.assertEqual(report["udiff"]["summary"]["quote_like_columns_anywhere"], [])
        self.assertEqual(report["schema"], inv.SCHEMA)

    def test_a_bid_or_ask_column_would_be_noticed(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01", BestBid="1")], header=HEADER + ["BestBid"])
        self.assertEqual(self.build()["udiff"]["summary"]["quote_like_columns_anywhere"], ["BestBid"])

    def test_copies_in_different_wrappers_are_duplicates_not_conflicts(self):
        rows = [row("2026-06-01")]
        write_csv(self.root, "2026-06-01", rows)
        write_zip(self.root, "2026-06-01", rows, sub="elsewhere")
        s = self.build()["udiff"]["summary"]
        self.assertEqual((s["dates_held_in_more_than_one_copy"], s["dates_with_conflicting_copies"], s["distinct_trade_dates"], s["file_count"]), (["2026-06-01"], [], 1, 2))
        files = self.build()["udiff"]["files"]
        self.assertEqual(files[0]["sha256_content"], files[1]["sha256_content"])
        self.assertNotEqual(files[0]["sha256_file"], files[1]["sha256_file"])

    def test_copies_that_differ_are_reported_as_a_conflict(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01", close="100.00")])
        write_csv(self.root, "2026-06-01", [row("2026-06-01", close="101.00")], sub="other")
        self.assertEqual(self.build()["udiff"]["summary"]["dates_with_conflicting_copies"], ["2026-06-01"])

    def test_a_file_whose_inner_date_disagrees_with_its_name_is_flagged(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-02")])
        self.assertEqual(self.build()["udiff"]["summary"]["files_whose_inner_date_differs_from_name"], [name_for("2026-06-01")])

    def test_missing_weekdays_are_listed_and_weekends_are_not(self):
        for date in ("2026-06-01", "2026-06-04", "2026-06-08"):                 # Monday, Thursday, Monday
            write_csv(self.root, date, [row(date)])
        s = self.build()["udiff"]["summary"]
        self.assertEqual(s["missing_weekdays_between_first_and_last"], ["2026-06-02", "2026-06-03", "2026-06-05"])
        self.assertEqual((s["first_date"], s["last_date"], s["distinct_trade_dates"]), ("2026-06-01", "2026-06-08", 3))

    def test_a_reused_instrument_token_is_found_and_a_contract_with_two_tokens_too(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01", strike="25000.00", token="500"), row("2026-06-01", strike="26000.00", token="600")])
        write_csv(self.root, "2026-06-02", [row("2026-06-02", strike="25000.00", token="501"), row("2026-06-02", strike="27000.00", token="500")])
        s = self.build()["udiff"]["summary"]
        self.assertEqual(s["tokens_shared_by_more_than_one_contract"], ["500"])         # 500 was 25000 CE one day and 27000 CE the next
        self.assertEqual(len(s["contracts_with_more_than_one_token"]), 1)               # 25000 CE was 500 then 501

    def test_bad_zips_are_reported_not_crashed_on(self):
        (self.root / (name_for("2026-06-01") + ".zip")).write_bytes(b"not a zip")
        with zipfile.ZipFile(self.root / (name_for("2026-06-02") + ".zip"), "w") as archive:
            archive.writestr("a.csv", "x")
            archive.writestr("b.csv", "y")
        s = self.build()["udiff"]["summary"]
        self.assertEqual(sorted(s["unreadable_files"]), sorted([name_for("2026-06-01") + ".zip", name_for("2026-06-02") + ".zip"]))
        self.assertEqual(s["distinct_trade_dates"], 0)

    def test_contract_wise_files_are_parsed_with_their_stray_spaces_and_dashes(self):
        text = ("Symbol  ,Date  ,Expiry  ,Option type  ,Strike Price  ,Open  ,High  ,Low  ,Close  ,LTP  ,Settle Price  ,No. of contracts  ,Turnover * in  Rs Lakhs,"
                "Premium Turnover ** in   Rs Lakhs,Open Int  ,Change in OI  ,Underlying Value  \n"
                "NIFTY,26-Dec-2025,29-Sep-2026,CE,25000.00,2670.00,2670.00,2670.00,2670.00,2670.00,2365.85,1.00,20.75,2.00,-,-75.00,26042.30\n"
                "NIFTY,29-Dec-2025,29-Sep-2026,CE,31000.00,-,-,-,272.80,-,129.65,-,-,-,-,-,26042.30\n")
        (self.root / "OPTIDX_NIFTY_CE_01-Dec-2025_TO_29-Dec-2025.csv").write_text(text, encoding="utf-8")
        f, = self.build()["optidx"]["files"]
        self.assertEqual((f["rows"], f["first_date"], f["last_date"], f["expiry_min"], f["expiry_max"], f["option_types"]), (2, "2025-12-26", "2025-12-29", "2026-09-29", "2026-09-29", {"CE": 2}))
        self.assertEqual((f["rows_with_dash_as"]["Open"], f["rows_with_dash_as"]["LTP"], f["rows_with_dash_as"]["Close"] if "Close" in f["rows_with_dash_as"] else 0), (1, 1, 0))
        self.assertEqual(f["quote_like_columns"], [])

    def test_the_tool_never_changes_the_folder_it_reads(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01")])
        write_zip(self.root, "2026-06-02", [row("2026-06-02")])
        (self.root / "notes.txt").write_text("x")
        before = tree_state(self.root)
        self.build()
        out = Path(self.tmp.name) / "report.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(inv.main([str(self.root), "--out", str(out), "--compact"]), 0)
            self.assertEqual(inv.main([str(self.root)]), 0)
        self.assertEqual(tree_state(self.root), before)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["compact"], True)

    def test_it_refuses_to_write_its_report_inside_the_folder_it_inventories(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01")])
        before = tree_state(self.root)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(inv.main([str(self.root), "--out", str(self.root / "report.json")]), 2)
            self.assertEqual(inv.main([str(self.root), "--out", str(self.root / "sub" / "report.json")]), 2)
        self.assertIn("outside the folder", err.getvalue())
        self.assertEqual(tree_state(self.root), before)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(inv.main([str(self.root / "missing")]), 2)

    def test_the_tool_source_has_no_way_to_write_rename_or_delete_inside_the_data_folder(self):
        text = (ROOT / "tools" / "inventory_bhavcopy.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"open\([^)]*[\"'][wax+][bt]?[\"']", text))
        for word in ("os.remove", "os.rename", "unlink", "shutil", "rmtree", "os.replace", "write_bytes", "mkdir", "urllib", "socket", "requests"):
            self.assertNotIn(word, text)
        self.assertEqual(text.count("write_text"), 1)                         # only the optional --out report

    def test_output_is_deterministic_apart_from_the_timestamp(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01")])
        write_zip(self.root, "2026-06-02", [row("2026-06-02")])
        a, b = self.build(), self.build()
        a.pop("generated_utc")
        b.pop("generated_utc")
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_other_files_and_cache_folders_are_ignored(self):
        write_csv(self.root, "2026-06-01", [row("2026-06-01")])
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__" / name_for("2026-06-05")).write_text("ignored")
        (self.root / "README.md").write_text("x")
        report = self.build()
        self.assertEqual((report["udiff"]["summary"]["file_count"], report["ignored_files"]), (1, 1))


def load_json_strict(path):
    def no_duplicates(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate key in {path}: {sorted(k for k in set(keys) if keys.count(k) > 1)}")
        return dict(pairs)
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)


class TestSourceRegistry(unittest.TestCase):
    path = ROOT / "config" / "historical_data_sources.json"

    def setUp(self):
        self.registry = load_json_strict(self.path)

    def claims(self, source):
        for group in ("capabilities", "history"):
            for name, claim in source.get(group, {}).items():
                yield f"{source['id']}.{group}.{name}", claim
        for name in ("cost", "account_required"):
            if name in source:
                yield f"{source['id']}.{name}", source[name]

    def test_structure_and_unique_ids(self):
        self.assertEqual(self.registry["schema"], "niftypaper.data_source_registry/1")
        ids = [s["id"] for s in self.registry["sources"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]+", i) for i in ids))
        for source in self.registry["sources"]:
            for key in ("id", "name", "publisher", "access", "terms", "supports", "limitations", "enabled_for_v6"):
                self.assertIn(key, source, source["id"])
            self.assertTrue(source["limitations"] and all(isinstance(x, str) and x for x in source["limitations"]))

    def test_nothing_is_enabled(self):
        self.assertTrue(all(s["enabled_for_v6"] is False for s in self.registry["sources"]))

    def test_every_claim_says_how_it_is_known(self):
        allowed = set(self.registry["status_values"])
        self.assertEqual(allowed, {"verified", "assumed", "unknown"})
        count = 0
        for label, claim in (c for s in self.registry["sources"] for c in self.claims(s)):
            count += 1
            self.assertIn(claim.get("status"), allowed, label)
            self.assertIn("value", claim, label)
            self.assertTrue(isinstance(claim.get("evidence"), str) and len(claim["evidence"]) >= 12, label)
        self.assertGreater(count, 25)

    def test_terms_are_recorded_with_a_date_and_a_link(self):
        for source in self.registry["sources"]:
            terms = source["terms"]
            for key in ("url", "checked_on", "automated_access", "virtual_trading_or_simulation_use"):
                self.assertIn(key, terms, source["id"])
            self.assertRegex(terms["checked_on"], r"^\d{4}-\d{2}-\d{2}$")

    def test_no_nse_website_source_is_marked_as_permitted_for_simulation(self):
        """A guard against someone flipping a legal question into a yes without evidence: the text found says the opposite."""
        for source in self.registry["sources"]:
            use = source["terms"]["virtual_trading_or_simulation_use"]
            if source["publisher"].startswith("NSE") or source["id"].startswith("nse_"):
                self.assertNotEqual(use, "permitted", source["id"])
        udiff = next(s for s in self.registry["sources"] if s["id"] == "nse_udiff_bhavcopy")
        self.assertTrue(any("virtual trading or simulation" in c for c in udiff["terms"]["quoted_clauses"]))

    def test_support_values_are_from_a_fixed_set_and_no_source_claims_realistic_intraday_replay(self):
        for source in self.registry["sources"]:
            self.assertEqual(set(source["supports"]), {"settlement_research", "eod_replay", "intraday_replay"}, source["id"])
            for value in source["supports"].values():
                self.assertIn(value, {"yes", "no", "partial", "unknown"})
            self.assertNotEqual(source["supports"]["intraday_replay"], "yes", source["id"])        # nothing verified supports it
        for source in self.registry["sources"]:
            if source.get("capabilities", {}).get("bid_ask", {}).get("value") == "no":
                self.assertEqual(source["supports"]["intraday_replay"], "no", source["id"])           # no bid/ask means no realistic fills

    def test_the_file_holds_no_secret_or_order_pattern(self):
        findings = scan_text("config/historical_data_sources.json", self.path.read_text(encoding="utf-8"))
        self.assertEqual([str(f) for f in findings], [])


class TestCanonicalSchema(unittest.TestCase):
    path = ROOT / "config" / "historical_schema_v1.json"

    def setUp(self):
        self.schema = load_json_strict(self.path)
        self.kinds = self.schema["record_kinds"]

    def test_only_quote_snapshots_can_be_fill_sources(self):
        self.assertEqual({k for k, v in self.kinds.items() if v["fill_source"]}, {"QUOTE_SNAPSHOT"})
        self.assertEqual(set(self.kinds), {"EOD_CONTRACT_DAY", "INTRADAY_BAR", "QUOTE_SNAPSHOT", "UNDERLYING_EOD", "CONTRACT_SPEC", "EXCLUSION"})

    def test_settlement_bar_and_index_records_have_no_bid_or_ask_field_at_all(self):
        quote_words = {"bid", "ask", "bid_qty", "ask_qty", "depth"}
        for kind, definition in self.kinds.items():
            if kind == "QUOTE_SNAPSHOT":
                continue
            self.assertEqual(quote_words & set(definition["fields"]), set(), kind)
        for kind in ("EOD_CONTRACT_DAY", "INTRADAY_BAR", "UNDERLYING_EOD"):
            self.assertTrue({"bid", "ask"} <= set(self.kinds[kind]["forbidden_fields"]), kind)
        self.assertIn("settlement_price", self.kinds["INTRADAY_BAR"]["forbidden_fields"])

    def test_end_of_day_records_say_plainly_that_they_have_no_time_of_day(self):
        eod = self.kinds["EOD_CONTRACT_DAY"]["fields"]
        self.assertEqual((eod["observation_precision"]["fixed"], eod["observed_at"]["fixed"], eod["observed_at"]["nullable"]), ("DAY", None, True))
        self.assertIn("23:59:59 IST", eod["available_at"]["rule"])
        self.assertEqual((eod["tick_size"]["nullable"], eod["lot_size"]["nullable"]), (True, True))

    def test_a_quote_cannot_exist_without_the_things_a_fill_needs(self):
        fields = self.kinds["QUOTE_SNAPSHOT"]["fields"]
        for name in ("contract_key", "observed_at", "time_semantics", "available_at", "bid", "ask", "lot_size", "tick_size"):
            self.assertFalse(fields[name]["nullable"], name)
        self.assertIn("QUOTE_TIME", self.kinds["QUOTE_SNAPSHOT"]["fill_eligibility_rule"])
        self.assertIn("V6-0.1", self.kinds["QUOTE_SNAPSHOT"]["fill_eligibility_rule"])

    def test_every_field_is_typed_and_every_enum_and_flag_it_names_exists(self):
        enums = self.schema["enums"]
        for kind, definition in self.kinds.items():
            for name, field in definition["fields"].items():
                self.assertIn("type", field, f"{kind}.{name}")
                self.assertIsInstance(field["nullable"], bool, f"{kind}.{name}")
                ref = re.fullmatch(r"(?:array<)?enum:(\w+)>?", field["type"])
                if ref:
                    self.assertIn(ref.group(1), enums, f"{kind}.{name}")
                if "flag" in field:
                    self.assertIn(field["flag"], enums["quality_flags"], f"{kind}.{name}")
        self.assertEqual(set(self.schema["common_provenance_fields"]) >= {"source_id", "source_file", "source_file_sha256", "retrieved_at_utc", "parser_version", "quality_status", "quality_flags"}, True)
        self.assertEqual(set(self.kinds["EXCLUSION"]["fields"]["reason"]["type"].split(":")), {"enum", "exclusion_reason"})

    def test_source_columns_named_in_the_schema_exist_in_the_real_layout(self):
        spec_columns = {
            "underlying": "TckrSymb", "expiry": "XpryDt", "strike": "StrkPric", "option_type": "OptnTp", "source_instrument_id": "FinInstrmId", "source_instrument_name": "FinInstrmNm",
            "trading_date": "TradDt", "last_price": "LastPric", "underlying_value": "UndrlygPric", "volume": "TtlTradgVol", "transactions": "TtlNbOfTxsExctd", "lot_size": "NewBrdLotQty",
        }
        eod = self.kinds["EOD_CONTRACT_DAY"]["fields"]
        for field, column in spec_columns.items():
            self.assertIn(column, eod[field]["source"], field)
            self.assertIn(column, inv.UDIFF_COLUMNS, column)

    def test_the_volume_unit_trap_is_written_down(self):
        eod = self.kinds["EOD_CONTRACT_DAY"]["fields"]
        self.assertEqual(eod["volume_unit"]["values"], ["CONTRACTS", "UNITS", "UNKNOWN"])
        self.assertIn("VOLUME_UNIT_INFERRED", self.schema["enums"]["quality_flags"])
        self.assertIn("TURNOVER_MEANING_UNVERIFIED", self.schema["enums"]["quality_flags"])

    def test_the_duplicate_policy_excludes_conflicts_and_never_prefers_a_copy(self):
        policy = self.schema["duplicate_and_conflict_policy"]
        self.assertIn("Exclude ALL", policy["conflicting_copies"])
        self.assertIn("never prefer", policy["conflicting_copies"])
        self.assertIn("never merged", policy["across_sources"])
        self.assertTrue({"DUPLICATE_OF_EARLIER_COPY", "CONFLICTING_COPIES", "HASH_MISMATCH_WITH_MANIFEST"} <= set(self.schema["enums"]["exclusion_reason"]))

    def test_the_contract_key_format_and_example_agree(self):
        self.assertRegex(self.schema["contract_key"]["example"], r"^[A-Z0-9]+\|\d{4}-\d{2}-\d{2}\|\d+\.\d{2}\|(CE|PE)$")
        self.assertIn("NOT a key", self.schema["contract_key"]["note"])

    def test_the_raw_store_stays_out_of_the_research_folder_and_under_ignored_data(self):
        layout = self.schema["raw_store"]["layout"]
        self.assertTrue(layout.startswith("data/raw/"))
        self.assertIn("never under the research folder", " ".join(self.schema["raw_store"]["rules"]))
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("data/", gitignore)

    def test_the_schema_is_a_draft_implemented_on_fixtures_only(self):
        self.assertIn("fixtures only", self.schema["status"])
        self.assertTrue(self.schema["version"].endswith("draft"))


class TestCommittedInventory(unittest.TestCase):
    """The inventory kept in docs/ was produced by the tool; check it still says what the report relies on."""
    path = ROOT / "docs" / "historical_data" / "existing_data_inventory.json"

    def test_its_headline_facts(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        s = data["udiff"]["summary"]
        self.assertEqual((data["schema"], data["compact"], s["distinct_trade_dates"], s["first_date"], s["last_date"]), (inv.SCHEMA, True, 133, "2026-02-23", "2026-09-29"))
        self.assertEqual((s["dates_with_conflicting_copies"], s["quote_like_columns_anywhere"], s["nifty_lot_sizes_seen"], s["all_files_have_expected_columns"]), ([], [], ["65"], True))
        self.assertGreater(len(s["tokens_shared_by_more_than_one_contract"]), 0)
        self.assertEqual(s["contracts_with_more_than_one_token"], [])
        self.assertEqual(data["optidx"]["summary"]["quote_like_columns_anywhere"], [])
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", f["sha256_file"]) for f in data["udiff"]["files"]))
        self.assertNotIn("Users", json.dumps(data))                         # no local absolute paths


if __name__ == "__main__":
    unittest.main()
