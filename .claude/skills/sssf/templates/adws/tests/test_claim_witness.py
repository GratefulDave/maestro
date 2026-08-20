"""A claim no fixture the gate runs could witness is refused before it ships.

Recorded case, plan `cmo-consolidation-l-r6`, lane `lane-p5-gap-policy`. The
requirement says the module "persists a per-source cursor, the last successful
entry and date, a failure count, the next retry, and the source response
fingerprint", and in the same paragraph says "its gate runs entirely against
the in-memory SQLite session fixture and those injected fakes". Its claim
asserts, among nine behaviours, "that the cursor persists the last successful
entry and date". Read as survival across a process boundary, that is a fact the
declared gate cannot show: the store dies when the invocation does, so the
builder writes the behaviour, the gate observes nothing about it, and the
reviewer is asked to judge what it never saw.

Nothing in the IR as authored says which reading is meant. The five `effects`
are external acts and name no store; `surface` names repository paths, not
runtime state; the `fixtures[]` records are static source evidence and the
runtime reads them nowhere. The only statement of the boundary was prose, and
§1.2 forbids a lifecycle transition caused by free text — an admission decision
is a lifecycle transition. So the refusal reads a typed cell the author writes,
`claims[].witness`, and never the claim's own words. The anti-lexical test at
the end of this module is the one that matters most: a plan whose every
free-text field asserts persistence in the strongest terms available is
admitted, because its declared cells say the behaviour is within one
invocation.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adw_modules import plan_validate as pv            # noqa: E402
from test_plan_admission import AdmissionTestCase, _ir  # noqa: E402


#: Every persistence phrase the p5 requirement, its claim, its seam and its
#: fixtures actually use, plus the ones a lexical check would be written to
#: catch. It appears in every free-text field the IR carries and refuses
#: nothing, which is the property this module exists to hold.
PERSISTENCE_PROSE = (
    "It persists a per-source cursor that survives restarts and survives "
    "process death, so a second invocation resumes from the checkpoint "
    "rather than repeating a full historical scan. The cursor is durable "
    "across runs, the ordering is cross-process and monotonic, delivery is "
    "exactly-once, and a replay after a crash reads back the last "
    "successful entry and date."
)

P5 = TESTS / "fixtures" / "cmo-consolidation-l-r6-p5.plan.json"

#: What "persists a per-source cursor" plus "its gate runs entirely against
#: the in-memory SQLite session fixture" states once it is typed.
P5_TYPED_INTENT = {"scope": "cross_invocation", "store": "in_memory"}


def _witness(ir: dict, index: int = 0, **witness) -> dict:
    ir["claims"][index]["witness"] = dict(witness)
    return ir


class ClaimWitnessTestCase(AdmissionTestCase):
    def unwitnessable(self, ir: dict):
        """The `CLAIM_UNWITNESSABLE` blockers, and only those."""
        return [item for item in self.blockers(ir)
                if item.obligation
                is pv.AdmissionObligation.CLAIM_UNWITNESSABLE]

    def assert_unwitnessable_at(self, ir: dict, pointer: str, *fragments):
        found = self.unwitnessable(ir)
        self.assertTrue(
            found,
            "expected a CLAIM_UNWITNESSABLE refusal, got: "
            + (" | ".join(str(item.obligation) + " " + item.message
                          for item in self.blockers(ir)) or "admission"))
        self.assertIn(pointer, [item.pointer for item in found],
                      " | ".join(item.pointer for item in found))
        joined = " | ".join(item.message for item in found)
        for fragment in fragments:
            self.assertIn(fragment, joined)
        return found

    def assert_no_unwitnessable(self, ir: dict):
        found = self.unwitnessable(ir)
        self.assertEqual(
            found, [],
            "expected no CLAIM_UNWITNESSABLE blocker, got: "
            + " | ".join(item.pointer + " " + item.message for item in found))


class TheControlIsAdmittedTest(ClaimWitnessTestCase):
    def test_the_control_is_admitted(self):
        """Every claim in the control carries a well-formed witness, so a
        refusal here means the fixture is wrong rather than the plan."""
        self.assert_admitted(_ir())

    def test_the_obligation_is_one_of_the_seven(self):
        self.assertIn(pv.AdmissionObligation.CLAIM_UNWITNESSABLE,
                      pv.ADMISSION_OBLIGATIONS)


class TheUnwitnessablePredicateTest(ClaimWitnessTestCase):
    """Arm two: a boundary the declared store cannot be read across."""

    def test_a_cross_invocation_claim_over_an_in_memory_store_is_refused(self):
        ir = _witness(_ir(), scope="cross_invocation", store="in_memory")
        found = self.assert_unwitnessable_at(
            ir, "/claims/0/witness",
            "claim-tables", "in_memory", "tmp_path", "cross_invocation")
        self.assertEqual(len(found), 1)

    def test_a_cross_invocation_claim_over_no_store_is_refused(self):
        ir = _witness(_ir(), scope="cross_invocation", store="none")
        self.assert_unwitnessable_at(ir, "/claims/0/witness", "claim-tables")

    def test_the_message_names_the_claim_the_store_and_the_remedy(self):
        ir = _witness(_ir(), scope="cross_invocation", store="in_memory")
        message = self.unwitnessable(ir)[0].message
        self.assertIn("claim-tables", message)
        self.assertIn("in_memory", message)
        for store in sorted(pv._WITNESSING_STORES):
            self.assertIn(store, message)

    def test_the_same_claim_over_a_tmp_path_store_is_admitted(self):
        """The pass: the claim asks for nothing else, and its fixtures can now
        observe the persistence it asserts."""
        self.assert_admitted(
            _witness(_ir(), scope="cross_invocation", store="tmp_path"))

    def test_every_witnessing_store_admits_a_cross_invocation_claim(self):
        for store in sorted(pv._WITNESSING_STORES):
            with self.subTest(store=store):
                self.assert_admitted(
                    _witness(_ir(), scope="cross_invocation", store=store))

    def test_an_in_process_claim_is_admitted_under_every_store(self):
        """An author who states the behaviour is within one invocation has
        stated something one command can check, whatever the store."""
        for store in pv.WITNESS_STORES:
            with self.subTest(store=store):
                self.assert_admitted(
                    _witness(_ir(), scope="in_process", store=store))

    def test_only_the_claim_that_declares_it_is_refused(self):
        ir = _ir()
        _witness(ir, 1, scope="cross_invocation", store="in_memory")
        found = self.assert_unwitnessable_at(ir, "/claims/1/witness",
                                             "claim-freeze")
        self.assertEqual(len(found), 1)


class TheOmissionArmTest(ClaimWitnessTestCase):
    """Arm one: a claim that does not state its boundary at all.

    Same shape as a requirement without `effects`. The field is required
    rather than optional-and-checked-when-present, on §3.6 B8: a field added
    later is optional forever, and a witness would then be declared by exactly
    the plans that already had one.
    """

    def test_a_claim_without_a_witness_is_refused(self):
        ir = _ir()
        del ir["claims"][0]["witness"]
        self.assert_unwitnessable_at(ir, "/claims/0/witness",
                                     "claim-tables", "declares no witness")

    def test_a_witness_that_is_not_an_object_is_refused(self):
        ir = _ir()
        ir["claims"][0]["witness"] = "cross_invocation, in_memory"
        self.assert_unwitnessable_at(ir, "/claims/0/witness",
                                     "not a {scope, store} object")

    def test_an_unknown_scope_is_refused_at_its_own_pointer(self):
        ir = _witness(_ir(), scope="cross_process", store="tmp_path")
        self.assert_unwitnessable_at(ir, "/claims/0/witness/scope",
                                     "cross_process", "in_process")

    def test_an_unknown_store_is_refused_at_its_own_pointer(self):
        ir = _witness(_ir(), scope="cross_invocation", store="sqlite")
        self.assert_unwitnessable_at(ir, "/claims/0/witness/store",
                                     "sqlite", "in_memory")

    def test_a_missing_key_is_refused_at_its_own_pointer(self):
        ir = _ir()
        ir["claims"][0]["witness"] = {"scope": "in_process"}
        self.assert_unwitnessable_at(ir, "/claims/0/witness/store", "None")

    def test_a_malformed_witness_does_not_also_raise_the_second_arm(self):
        """One authoring defect reported once. A scope outside the vocabulary
        is not silently read as `cross_invocation`."""
        ir = _witness(_ir(), scope="across_restarts", store="in_memory")
        found = self.unwitnessable(ir)
        self.assertEqual([item.pointer for item in found],
                         ["/claims/0/witness/scope"])

    def test_both_malformed_keys_are_reported_together(self):
        ir = _witness(_ir(), scope="nope", store="nope")
        found = self.unwitnessable(ir)
        self.assertEqual(sorted(item.pointer for item in found),
                         ["/claims/0/witness/scope", "/claims/0/witness/store"])

    def test_a_claim_that_is_not_an_object_is_refused(self):
        ir = _ir()
        ir["claims"][0] = "claim-tables"
        self.assert_unwitnessable_at(ir, "/claims/0", "witness")

    def test_a_plan_with_no_claims_key_raises_nothing_here(self):
        """`claims` is required by the authoring schema and `planctl validate`
        refuses a malformed one in its own vocabulary. Reporting the same
        defect twice makes one plan error look like two."""
        ir = _ir()
        del ir["claims"]
        self.assert_admitted(ir)


class TheRefusalsArriveTogetherTest(ClaimWitnessTestCase):
    def test_a_witness_blocker_arrives_beside_the_other_two_families(self):
        """§11.1: an author sent back three times for three defects in one
        document is the fail-fast validator this design refuses."""
        ir = _witness(_ir(), scope="cross_invocation", store="in_memory")
        ir["requirements"][1]["surface"].append(
            {"path": "src/legacy_writer.py", "mutation": "written"})
        for entry in ir["requirements"][0]["effects"]:
            if entry["effect"] == "source_backfill":
                entry["disposition"] = "performed"
        obligations = {item.obligation for item in self.blockers(ir)}
        self.assertIn(pv.AdmissionObligation.SURFACE_REACHABLE, obligations)
        self.assertIn(pv.AdmissionObligation.EFFECT_AUTHORIZED, obligations)
        self.assertIn(pv.AdmissionObligation.CLAIM_UNWITNESSABLE, obligations)


class TheDecisionReadsNoProseTest(ClaimWitnessTestCase):
    """The anti-lexical control, matching the two the other families carry.

    A lexicon over `requirements[].text`, `claims[].object`, or
    `verifiers[].oracle` would decide this case the other way, which is why
    there is no lexicon: an admission decision is a lifecycle transition, and
    §1.2 forbids one caused by model-readable text. Whether a declared
    `in_process` is *true* is the plan-contract reviewer's to falsify (§3.6
    B12) — a structural check cannot honestly claim more.
    """

    def _prose_everywhere(self) -> dict:
        ir = _ir()
        ir["title"] = PERSISTENCE_PROSE
        for requirement in ir["requirements"]:
            requirement["text"] = PERSISTENCE_PROSE
        for lane in ir["lanes"]:
            lane["title"] = PERSISTENCE_PROSE
        for verifier in ir["verifiers"]:
            verifier["oracle"] = PERSISTENCE_PROSE
            verifier["falsifiability"] = {
                "mutation": PERSISTENCE_PROSE,
                "expected_failure": PERSISTENCE_PROSE,
            }
        for claim in ir["claims"]:
            claim["subject"] = PERSISTENCE_PROSE
            claim["object"] = PERSISTENCE_PROSE
            claim["preconditions"] = [PERSISTENCE_PROSE]
            claim["postconditions"] = [PERSISTENCE_PROSE]
            claim["state_from"] = PERSISTENCE_PROSE
            claim["state_to"] = PERSISTENCE_PROSE
        ir["seams"] = [{"seam_id": "seam-freeze",
                        "producer": PERSISTENCE_PROSE,
                        "consumer": PERSISTENCE_PROSE,
                        "contract": PERSISTENCE_PROSE}]
        ir["fixtures"] = [{"fixture_id": "fx-cursor",
                           "path": PERSISTENCE_PROSE,
                           "observed_value": PERSISTENCE_PROSE,
                           "meaning": PERSISTENCE_PROSE,
                           "consumer_obligation": PERSISTENCE_PROSE,
                           "prohibited_behavior": PERSISTENCE_PROSE,
                           "affected_lane_ids": ["lane-freeze"]}]
        prohibited = ir["extensions"]["maestro"]["prohibited_effects"]
        for entry in prohibited:
            entry["meaning"] = PERSISTENCE_PROSE
        return ir

    def test_prose_asserting_persistence_everywhere_does_not_refuse(self):
        ir = self._prose_everywhere()
        for claim in ir["claims"]:
            self.assertEqual(claim["witness"]["scope"], "in_process")
        self.assert_admitted(ir)

    def test_a_claim_with_no_persistence_prose_is_still_refused(self):
        """The converse. The declared cells decide, so a claim whose every
        word is about a pure in-process assertion is refused when it says it
        crosses an invocation over a store that cannot be read twice."""
        ir = _ir()
        ir["claims"][0]["object"] = "The function returns 4 for 2 plus 2."
        _witness(ir, scope="cross_invocation", store="in_memory")
        self.assert_unwitnessable_at(ir, "/claims/0/witness")


class TheRecordedP5CaseTest(ClaimWitnessTestCase):
    """The real IR, reduced to the one lane and everything it references.

    `tests/fixtures/cmo-consolidation-l-r6-p5.plan.json` is
    `cmo-consolidation-l-r6.plan.json` from `lexgenius-pipeline` with
    `req-p5-gap-policy`, `claim-p5-gap-policy`, `lane-p5-gap-policy`,
    `verify-p5-gap-policy`, `seam-p5-gap-policy`, its two fixtures, the six
    source artifacts its traceability names, and its rendered binding kept
    byte-for-byte; the four other lanes and their records are dropped, and the
    integration gate's selector is reduced to this lane's because the shipped
    one names fourteen lanes' test files. Its `plan_id` reads
    `cmo-consolidation-l-r5` in the file named `-r6`, as found: plan identity
    is the digest, not the filename.
    """

    def ir(self) -> dict:
        return json.loads(P5.read_text(encoding="utf-8"))

    def test_the_fixture_is_the_authored_claim(self):
        claim = self.ir()["claims"][0]
        self.assertEqual(claim["claim_id"], "claim-p5-gap-policy")
        self.assertIn("the cursor persists the last successful entry and "
                      "date", claim["object"])
        self.assertIn("a second incremental run repeats no full historical "
                      "scan", claim["object"])
        self.assertIn("its gate runs entirely against the in-memory SQLite "
                      "session fixture",
                      self.ir()["requirements"][0]["text"])

    def test_as_written_it_is_refused_for_declaring_no_witness(self):
        """The bytes as authored carry no `witness` key at all, so this is the
        omission arm and not a typed contradiction. There is no typed
        contradiction in them to find: the five `effects` are external acts and
        name no store, `surface` names repository paths, and the only statement
        that the cursor outlives the invocation is prose. The refusal is that
        the author never said which boundary the claim is about."""
        ir = self.ir()
        self.assertNotIn("witness", ir["claims"][0])
        self.assert_unwitnessable_at(ir, "/claims/0/witness",
                                     "claim-p5-gap-policy",
                                     "declares no witness")

    def test_the_typed_intent_is_refused_on_the_substantive_arm(self):
        """"Persists a per-source cursor" plus "its gate runs entirely against
        the in-memory SQLite session fixture", once each half is a declared
        cell instead of a sentence."""
        ir = self.ir()
        ir["claims"][0]["witness"] = dict(P5_TYPED_INTENT)
        found = self.assert_unwitnessable_at(
            ir, "/claims/0/witness", "claim-p5-gap-policy", "in_memory",
            "cross_invocation", "tmp_path")
        self.assertEqual(len(found), 1)
        self.assertNotIn("declares no witness", found[0].message)

    def test_the_same_claim_over_a_witnessing_store_is_admitted(self):
        """The remedy the refusal names, applied: the cursor lives somewhere a
        second invocation can read back, and the whole reduced plan admits."""
        ir = self.ir()
        ir["claims"][0]["witness"] = dict(P5_TYPED_INTENT, store="tmp_path")
        self.assert_no_unwitnessable(ir)
        self.assertEqual(self.blockers(ir), ())

    def test_scoping_it_in_process_is_also_admitted(self):
        """The other remedy. "A second incremental run repeats no full
        historical scan" is observable inside one pytest process against one
        in-memory session, and an author who means that reading says so."""
        ir = self.ir()
        ir["claims"][0]["witness"] = {"scope": "in_process",
                                      "store": "in_memory"}
        self.assertEqual(self.blockers(ir), ())

    def test_the_reduction_kept_the_records_the_refusal_is_about(self):
        """A reduction that dropped the lane's own evidence would make the
        admission below a statement about a fixture rather than about the
        plan."""
        ir = self.ir()
        self.assertEqual([item["lane_id"] for item in ir["lanes"]],
                         ["lane-p5-gap-policy"])
        self.assertEqual([item["fixture_id"] for item in ir["fixtures"]],
                         ["fx-source-hierarchy-tiers", "fx-refetch-source-walk"])
        self.assertEqual(ir["verifiers"][0]["min_executed"], 9)
        self.assertEqual(
            sorted(entry["effect"] for entry
                   in ir["requirements"][0]["effects"]),
            sorted(pv.EFFECTS))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
