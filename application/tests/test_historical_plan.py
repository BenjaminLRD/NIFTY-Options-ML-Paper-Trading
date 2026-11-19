"""Replay intent-plan identity: canonical digest, the claim in the first event, and the audit's comparison with the ordered intents the events record.
Invented fixtures and temporary folders only."""
import contextlib
import dataclasses
import hashlib
import io
import json
import unittest
from datetime import timezone

from niftypaper.historical import audit, identity, replay
from tests import ROOT
from tests.historical_fixtures import CE
from tests.test_historical_audit import SCRIPT
from tests.test_historical_chain import lines_of
from tests.test_historical_inputs import Base
from tests.test_historical_replay import END, START, at

KNOWN_ANSWER_PLAN = "ad54086a7fa0d35c34795d9fe4a2985e3c4fbe659cbcedd2dfc46ba682905e7e"


def variant(script, index, **change):
    return [dataclasses.replace(i, **change) if n == index else i for n, i in enumerate(script)]


class TestPlanDigest(unittest.TestCase):
    def test_identical_plans_have_identical_digests_whatever_the_time_zone_spelling(self):
        a, b = identity.plan(SCRIPT), identity.plan(list(SCRIPT))
        self.assertEqual(a, b)
        shifted = [dataclasses.replace(i, at=i.at.astimezone(timezone.utc)) for i in SCRIPT]
        self.assertEqual(identity.plan(shifted)["digest"], a["digest"])
        self.assertEqual(len(a["digest"]), 64)

    def test_a_known_answer_pins_the_plan_format(self):
        plan = identity.plan([replay.Intent(at(10, 15, 2), CE, "BUY", 1), replay.Intent(at(10, 15, 3), CE, "SELL", 1)])
        self.assertEqual(plan["intents"][0], {"at": "2031-03-03T04:45:02+00:00", "contract": CE, "side": "BUY", "lots": 1})
        self.assertEqual((plan["version"], plan["until"]), ("1", "2031-03-03T04:45:03+00:00"))
        self.assertEqual(plan["digest"], KNOWN_ANSWER_PLAN)

    def test_changing_any_intent_field_changes_the_digest(self):
        base = identity.plan(SCRIPT)["digest"]
        for name, change in (("at", {"at": at(10, 15, 3)}), ("contract_key", {"contract_key": "OTHER"}), ("side", {"side": "SELL"}), ("lots", {"lots": 7})):
            with self.subTest(name):
                self.assertNotEqual(identity.plan(variant(SCRIPT, 0, **change))["digest"], base)

    def test_until_is_part_of_the_plan(self):
        self.assertNotEqual(identity.plan(SCRIPT, until=at(10, 30))["digest"], identity.plan(SCRIPT)["digest"])
        self.assertEqual(identity.plan(SCRIPT, until=SCRIPT[-1].at)["digest"], identity.plan(SCRIPT)["digest"])       # the default IS the last instant

    def test_order_matters_when_it_decides_execution_and_only_then(self):
        base = identity.plan(SCRIPT)["digest"]
        same_instant = [i for i in SCRIPT if i.at == at(10, 15, 31)]
        swapped = list(SCRIPT)
        swapped[1], swapped[2] = swapped[2], swapped[1]                              # two intents at one instant: their order decides which runs first
        self.assertEqual(len(same_instant), 2)
        self.assertNotEqual(identity.plan(swapped)["digest"], base)
        times = [i.at for i in SCRIPT]                                               # swapping when two different intents happen is a different plan
        crossed = [dataclasses.replace(i, at=times[n]) for n, i in enumerate(SCRIPT)]
        crossed[0], crossed[3] = dataclasses.replace(SCRIPT[3], at=times[0]), dataclasses.replace(SCRIPT[0], at=times[3])
        self.assertNotEqual(identity.plan(crossed)["digest"], base)
        shuffled = [SCRIPT[3], SCRIPT[0], SCRIPT[1], SCRIPT[2]] + SCRIPT[4:]         # list order between different instants does not change execution
        self.assertEqual(identity.plan(shuffled)["digest"], base)

    def test_adding_or_removing_an_intent_changes_the_digest(self):
        base = identity.plan(SCRIPT)["digest"]
        self.assertNotEqual(identity.plan(SCRIPT[:-1])["digest"], base)
        self.assertNotEqual(identity.plan(SCRIPT + [replay.Intent(at(10, 21), CE, "BUY", 1)])["digest"], base)
        self.assertNotEqual(identity.plan([])["digest"], base)
        self.assertEqual(identity.plan([])["until"], None)

    def test_malformed_intents_are_refused(self):
        for bad in (replay.Intent(at(10, 15).replace(tzinfo=None), CE, "BUY", 1), replay.Intent(at(10, 15), CE, "HOLD", 1), replay.Intent(at(10, 15), CE, "BUY", True),
                    replay.Intent(at(10, 15), CE, "BUY", 1.5), replay.Intent(at(10, 15), 7, "BUY", 1), "not an intent"):
            with self.subTest(bad), self.assertRaises(ValueError):
                identity.plan([bad])

    def test_the_plan_is_in_the_full_digest_and_not_in_the_account_binding(self):
        run_a = self.claim(SCRIPT)
        run_b = self.claim(SCRIPT[:-1])
        self.assertNotEqual(run_a["digest"], run_b["digest"])
        self.assertEqual(run_a["account_binding"], run_b["account_binding"])

    def claim(self, script):
        claim = dict(self.base_claim)
        claim["plan"] = identity.plan(script)
        return {**claim, "digest": identity.full_digest(claim), "account_binding": identity.binding_of(claim)}

    @property
    def base_claim(self):
        return {"version": "1", "inputs": {"x": 1}, "accounting": {"y": 2}, "window": {"start": "a", "end": "b"}}

    def test_a_claim_without_a_plan_hashes_as_before(self):
        claim = self.base_claim
        self.assertNotIn("plan", claim)
        self.assertEqual(identity.full_digest(claim), identity._hash(identity.DIGEST_DOMAIN, claim))


class TestRunnerPlan(Base):
    def test_the_first_event_carries_the_plan_and_the_log_records_the_same_intents(self):
        run = self.run_variant("a")
        events = self.objs(run)
        plan = events[0]["inputs"]["plan"]
        self.assertEqual(plan, identity.plan(SCRIPT))
        self.assertEqual([(e["contract"], e["side"], e["lots"]) for e in events if e["type"] == "INTENT_RECEIVED"], [(i["contract"], i["side"], i["lots"]) for i in plan["intents"]])
        self.assertEqual(events[0]["inputs"]["digest"], identity.full_digest(events[0]["inputs"]))

    def test_run_accepts_only_the_declared_plan_and_only_once(self):
        runner = self.open("a", intents=SCRIPT)
        with self.assertRaises(replay.ReplayRefused) as caught:
            runner.run(SCRIPT[:-1])
        self.assertEqual(caught.exception.code, "PLAN_MISMATCH")
        self.assertEqual([e["type"] for e in runner.events if e["type"] == "INTENT_RECEIVED"], [])
        runner.run(SCRIPT)
        with self.assertRaises(replay.ReplayRefused) as again:
            runner.run(SCRIPT)
        self.assertEqual(again.exception.code, "PLAN_ALREADY_RUN")

    def test_a_plan_that_cannot_run_is_refused_before_anything_is_created(self):
        for name, script, code in (("outside", [replay.Intent(at(11, 59), CE, "BUY", 1)], "PLAN_OUTSIDE_WINDOW"), ("bad", ["x"], "BAD_INTENT"),
                                   ("side", [replay.Intent(at(10, 15, 2), CE, "HOLD", 1)], "BAD_INTENT")):
            with self.subTest(name), self.assertRaises(replay.ReplayRefused) as caught:
                replay.ReplayRunner.open(self.config(name), intents=script)
            self.assertEqual(caught.exception.code, code)
            self.assertFalse((self.tmp / name).exists())

    def test_a_plan_changes_the_digest_but_not_the_state_binding(self):
        a, b = self.open("a", intents=SCRIPT), self.open("b", intents=SCRIPT[:3])
        self.assertNotEqual(a.inputs["digest"], b.inputs["digest"])
        self.assertEqual(a.inputs["account_binding"], b.inputs["account_binding"])

    def test_without_a_plan_the_identity_states_none(self):
        runner = self.open("a")
        self.assertNotIn("plan", runner.inputs)
        self.assertEqual(runner.inputs["digest"], identity.full_digest(runner.inputs))


class TestAuditPlan(Base):
    def setUp(self):
        super().setUp()
        self.run_ = self.run_variant("main")

    def forged(self, name, change, resign=True):
        """The run's events with the claim edited, the claim's own digests recomputed (unless resign is False) and the whole chain rebuilt: a writer's best effort."""
        def edit(objs):
            claim = objs[0]["inputs"]
            change(claim)
            if resign:
                claim["digest"] = identity.full_digest(claim)
        return self.rechained(self.run_, name, edit)

    def plan_of(self, report):
        return report["inputs_identity"]["plan"]

    def test_a_clean_run_matches_in_full(self):
        report = self.report(self.run_)
        self.assertEqual((report["verdict"], report["coverage"], self.plan_of(report)["status"]), ("CONSISTENT", "FULL", "MATCH"))
        self.assertEqual((self.plan_of(report)["claimed_intents"], self.plan_of(report)["observed_intents"]), (len(SCRIPT), len(SCRIPT)))
        self.assertEqual(self.plan_of(report)["claimed_digest"], self.plan_of(report)["recomputed_digest"])
        self.assertEqual(self.status(report, "PL1_PLAN_IDENTITY"), "PASS")

    def test_a_log_claiming_one_plan_but_holding_another_plans_intents_fails_though_the_chain_is_valid(self):
        other = identity.plan(variant(SCRIPT, 1, lots=3))
        forged = self.forged("other", lambda claim: claim.update(plan=other))
        report = self.report(forged)
        self.assertEqual(report["chain"]["status"], "VALID")
        self.assertEqual((self.plan_of(report)["status"], self.status(report, "PL1_PLAN_IDENTITY"), report["verdict"]), ("MISMATCH", "FAIL", "INCONSISTENT"))
        self.assertIn("intent 2", self.notes(report, "PL1_PLAN_IDENTITY"))
        self.assertEqual(self.status(report, "X1_EVENT_TRADE_MATCH"), "PASS")                                       # accounting is a separate answer

    def test_a_plan_digest_edited_without_its_intents_fails(self):
        report = self.report(self.forged("digest", lambda claim: claim["plan"].update(digest="0" * 64)))
        self.assertEqual(self.plan_of(report)["status"], "MISMATCH")
        self.assertIn("does not follow from the plan's own intents", self.notes(report, "PL1_PLAN_IDENTITY"))

    def test_a_claim_whose_digest_contradicts_its_parts_fails_the_input_identity_check(self):
        report = self.report(self.forged("selfcontradiction", lambda claim: claim["plan"]["intents"][0].update(lots=9), resign=False))
        self.assertEqual(self.status(report, "I1_INPUT_IDENTITY"), "FAIL")
        self.assertIn("contradicts itself", self.notes(report, "I1_INPUT_IDENTITY"))

    def test_extra_missing_and_reordered_intents_are_each_caught(self):
        def with_plan(script_change):
            def change(claim):
                intents = claim["plan"]["intents"]
                script_change(intents)
                claim["plan"]["digest"] = identity.plan_digest({k: claim["plan"][k] for k in ("version", "intents", "until")})
            return change

        cases = (("extra_planned", lambda i: i.append({"at": "2031-03-03T04:51:00+00:00", "contract": CE, "side": "BUY", "lots": 1}), "records 7 of the 8"),
                 ("fewer_planned", lambda i: i.pop(), "an intent beyond the 6"),
                 ("same_instant_swapped", lambda i: i.__setitem__(slice(1, 3), [i[2], i[1]]), "intent 2"),
                 ("other_side", lambda i: i[0].update(side="SELL"), "intent 1"))
        for name, edit, text in cases:
            with self.subTest(name):
                report = self.report(self.forged(name, with_plan(edit)))
                self.assertEqual((self.plan_of(report)["status"], report["verdict"]), ("MISMATCH", "INCONSISTENT"), name)
                self.assertIn(text, self.notes(report, "PL1_PLAN_IDENTITY"))

    def replanned(self, name, edit):
        def change(claim):
            edit(claim["plan"])
            claim["plan"]["digest"] = identity.plan_digest({k: claim["plan"][k] for k in ("version", "intents", "until")})
        return self.report(self.forged(name, change))

    def test_times_order_and_until_are_checked_against_the_events(self):
        shifted = self.replanned("shifted", lambda p: p["intents"][0].update(at="2031-03-03T04:45:03+00:00"))             # same order, the first intent a second late
        self.assertIn("intent 1", self.notes(shifted, "PL1_PLAN_IDENTITY"))
        swapped = self.replanned("timeorder", lambda p: p["intents"].__setitem__(slice(0, 4), [p["intents"][3], p["intents"][1], p["intents"][2], p["intents"][0]]))
        self.assertIn("not in execution (time) order", self.notes(swapped, "PL1_PLAN_IDENTITY"))
        early = self.replanned("until", lambda p: p.update(until="2031-03-03T04:45:03+00:00"))
        self.assertIn("later than the plan's until", self.notes(early, "PL1_PLAN_IDENTITY"))

    def test_an_intent_run_outside_the_plan_is_caught(self):
        runner = self.open("extra", intents=SCRIPT)
        runner.run(SCRIPT)
        runner.try_trade(replay.Intent(at(10, 21), CE, "BUY", 1))
        runner.write_events(self.tmp / "extra" / "events.jsonl")
        runner.close()
        run = type("Run", (), {"events": self.tmp / "extra" / "events.jsonl", "state": self.tmp / "extra" / "state" / f"replay_{runner.source_id}.jsonl"})
        report = self.report(run)
        self.assertEqual(self.plan_of(report)["status"], "MISMATCH")
        self.assertIn("beyond", self.notes(report, "PL1_PLAN_IDENTITY"))

    def test_a_malformed_plan_is_a_failure_not_a_crash(self):
        for name, change in (("notlist", lambda c: c["plan"].update(intents="x")), ("noextra", lambda c: c["plan"]["intents"][0].pop("lots")),
                             ("none", lambda c: c["plan"]["intents"].__setitem__(0, None)), ("badtime", lambda c: c["plan"]["intents"][0].update(at="soon")),
                             ("outside", lambda c: c["window"].update(end="2031-03-03T04:45:03+00:00"))):
            with self.subTest(name):
                report = self.report(self.forged(name, change))
                self.assertIn(self.status(report, "PL1_PLAN_IDENTITY"), ("FAIL", "BLOCKED"))
                self.assertNotEqual(report["verdict"], "CONSISTENT")

    def test_an_unfinished_log_matches_as_far_as_it_goes_and_is_not_a_pass(self):
        lines = lines_of(self.run_.events.read_bytes())
        cut = next(n for n, x in enumerate(lines) if json.loads(x)["type"] == "INTENT_RECEIVED" and json.loads(x)["lots"] == 2)
        path = self.tmp / "prefix.jsonl"
        path.write_bytes(("\n".join(lines[:cut]) + "\n").encode("utf-8"))
        report = self.report(type("Run", (), {"events": path, "state": self.run_.state}))
        self.assertEqual(self.plan_of(report)["status"], "INCOMPLETE")
        self.assertEqual(self.status(report, "PL1_PLAN_IDENTITY"), "NOT_CHECKED")
        self.assertEqual(report["coverage"], "PARTIAL")

    def test_legacy_logs_without_a_plan_are_not_declared_and_never_matching(self):
        runner = self.open("nop")
        runner.run(SCRIPT)
        runner.write_events(self.tmp / "nop" / "events.jsonl")
        runner.close()
        run = type("Run", (), {"events": self.tmp / "nop" / "events.jsonl", "state": self.tmp / "nop" / "state" / f"replay_{runner.source_id}.jsonl"})
        report = self.report(run)
        self.assertEqual((report["verdict"], report["coverage"], self.plan_of(report)["status"]), ("CONSISTENT", "PARTIAL", "NOT_DECLARED"))
        self.assertEqual(self.status(report, "PL1_PLAN_IDENTITY"), "NOT_CHECKED")
        self.assertEqual(self.plan_of(report)["claimed_digest"], {"unavailable": "NOT_RECORDED"})

    def test_a_log_with_no_identity_at_all_is_still_handled(self):
        stripped = self.rechained(self.run_, "stripped", lambda objs: objs[0].pop("inputs"))
        report = self.report(stripped)
        self.assertEqual((self.plan_of(report)["status"], self.status(report, "PL1_PLAN_IDENTITY")), ("NOT_DECLARED", "NOT_CHECKED"))
        self.assertEqual(report["pairing"]["status"], "NOT_ESTABLISHED")

    def test_state_from_a_different_plan_is_not_blocked_by_the_binding_but_is_caught_by_the_trades(self):
        other = self.run_variant("other_plan", script=SCRIPT[:1] + [replay.Intent(at(10, 15, 31), CE, "BUY", 4)])
        report = self.report(type("Run", (), {"events": self.run_.events, "state": other.state}))
        self.assertEqual(report["pairing"]["status"], "PAIRED")                       # the binding covers inputs and settings, not the plan (documented)
        self.assertEqual((report["verdict"], self.status(report, "X1_EVENT_TRADE_MATCH")), ("INCONSISTENT", "FAIL"))
        self.assertEqual(self.plan_of(report)["status"], "MATCH")                    # the events agree with their own plan; the state does not agree with the events

    def test_separate_statuses_stay_separate(self):
        report = self.report(self.forged("sep", lambda claim: claim.update(plan=identity.plan(SCRIPT[:-1]))))
        self.assertEqual((report["chain"]["status"], report["pairing"]["status"], report["anchor"]["status"], report["anchor"]["authenticity"]),
                         ("VALID", "PAIRED", "NOT_SUPPLIED", "NOT_ESTABLISHED"))
        self.assertEqual(self.status(report, "A1_CASH"), "PASS")


class TestMidRunChange(Base):
    def test_a_change_seen_at_any_recheck_blocks_success_even_if_put_back(self):
        folder = self.fixture_copy("flip")
        runner = self.open("flip", folder, intents=SCRIPT)
        quotes = folder / "SYNTHETIC_quotes.jsonl"
        original = quotes.read_bytes()
        runner.run(SCRIPT)
        quotes.write_bytes(original + b"\n")
        self.assertTrue(runner.recheck_inputs())
        quotes.write_bytes(original)                                                   # put back before the final recheck
        with self.assertRaises(replay.InputsChanged):
            runner.write_events(self.tmp / "flip" / "events.jsonl")
        self.assertIsNone(runner.log_receipt)
        self.assertIsNotNone(runner.evidence_receipt)
        report = audit.build_report(audit.AuditConfig(self.tmp / "flip" / "events.jsonl", self.tmp / "flip" / "state" / f"replay_{runner.source_id}.jsonl", folder))
        self.assertEqual(self.status(report, "I1_INPUT_IDENTITY"), "FAIL")
        self.assertIn("earlier recheck", self.notes(report, "I1_INPUT_IDENTITY"))
        runner.close()


class TestDemoPlan(Base):
    def setUp(self):
        super().setUp()
        spec = __import__("importlib.util").util.spec_from_file_location("demo_plan", ROOT / "tools" / "replay_fixture_demo.py")
        self.demo = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(self.demo)

    def go(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.demo.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_demo_declares_its_plan_is_deterministic_and_never_overwrites(self):
        results = [self.go("--out-dir", str(self.tmp / name)) for name in ("one", "two")]
        for code, out, err in results:
            self.assertEqual((code, err), (0, ""))
            self.assertIn("intent plan   MATCH (7 intents, digest ", out)
        logs = [(self.tmp / n / "events.jsonl").read_bytes() for n in ("one", "two")]
        self.assertEqual(hashlib.sha256(logs[0]).hexdigest(), hashlib.sha256(logs[1]).hexdigest())
        first = json.loads(lines_of(logs[0])[0])
        self.assertEqual(first["inputs"]["plan"]["digest"], identity.plan(self.demo.script())["digest"])
        before = {p: p.read_bytes() for p in (self.tmp / "one").rglob("*") if p.is_file()}
        code, out, err = self.go("--out-dir", str(self.tmp / "one"))
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(before, {p: p.read_bytes() for p in (self.tmp / "one").rglob("*") if p.is_file()})

    def test_a_plan_the_audit_does_not_confirm_is_not_success(self):
        real = audit.build_report

        def patched(config):
            report = json.loads(json.dumps(real(config)))
            report["inputs_identity"]["plan"].update(status="MISMATCH", differences=["intent 2 differs"])
            return report

        from unittest import mock
        with mock.patch.object(audit, "build_report", patched):
            code, out, err = self.go("--out-dir", str(self.tmp / "p"))
        self.assertEqual((code, out), (4, ""))
        self.assertIn("the intent plan is MISMATCH", err)


if __name__ == "__main__":
    unittest.main()
