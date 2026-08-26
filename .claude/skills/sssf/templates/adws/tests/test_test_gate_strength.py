"""Executable gate strength for every test node.

Run `run-8d1a71f463e4430f92a125a8f8b3731d` is the measured case behind this
file. `lane-acquisition-manifest-tests` reached MERGED on four non-skipped
cases, and every one of the four implementation candidates its tests existed
to gate was independently rejected. The tests node's acceptance was therefore
supported by no evidence that it could discriminate: a passing command, a case
count, and a syntactically valid file are all compatible with a suite that
asserts nothing the contract cares about.

What is asserted here is the replacement, in the order the prompt's
verification requirements state it:

  1. enough executed cases but a missing negative obligation is refused
  2. a planted selector that matches no real case is refused
  3. tests that survive the declared negative control are refused
  4. an unrelated import/collection failure is not falsifiability proof
  5. an accepted test candidate is the implementation's exact prerequisite
  6. substituted test bytes cannot reuse an earlier acceptance
  7. a completed test review is not redispatched after resume
  8. rejection findings return to the retained tester's generation
  9. a stale callback cannot mutate the current lifecycle
 10. test and implementation merge only as the exact accepted pair
 11. a legacy MERGED tests node with no evidence is classified, not migrated
 12. the operator surfaces distinguish private acceptance from paired merge
 13. pytest and vitest are held to one invariant
 14. the two template copies stay level (`test_template_parity.py` owns it)

Run with:  uv run adws/adw_test.py -k test_gate_strength
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import plan_validate as pv  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

from test_scheduler import SchedulerFixture, green, red  # noqa: E402


# ── the contract a well-formed tests node declares ──────────────────────────


def obligation(requirement_id, aspect, selector, min_cases=1):
    return pm.CoverageObligation(
        requirement_id=requirement_id,
        aspect=aspect,
        case_selector=selector,
        min_cases=min_cases,
    )


def strength(
    coverage=None,
    strategy="baseline_absent",
    mutation=None,
    failing_selector="test_refund",
    reason=r"(AssertionError|ModuleNotFoundError|ImportError)",
):
    return pm.TestStrength(
        coverage=tuple(
            coverage
            if coverage is not None
            else (
                obligation("R1", "positive", "test_refund_pays_the_balance"),
                obligation("R1", "negative", "test_refund_rejects_a_negative_amount"),
            )
        ),
        falsifiability=pm.Falsifiability(
            strategy=strategy,
            mutation=mutation,
            expected_failing_selector=failing_selector,
            expected_reason_pattern=reason,
        ),
    )


def outcomes(*pairs):
    return tc.CaseRun(
        outcomes=tuple(
            tc.CaseOutcome(nodeid, status, reason)
            for nodeid, status, reason in pairs
        ),
        exit_code=0,
    )


# ── requirement 1: passing cases are not coverage ───────────────────────────


class CoverageIsMeasuredNotCounted(unittest.TestCase):
    def test_enough_cases_but_no_negative_obligation_is_refused(self):
        """Four green cases and no rejection case. This is the EPA shape."""
        contract = strength()
        run = outcomes(
            ("tests/t.py::test_refund_pays_the_balance", "failed", "AssertionError: x"),
            ("tests/t.py::test_refund_pays_a_partial", "failed", "AssertionError: x"),
            ("tests/t.py::test_refund_pays_zero", "failed", "AssertionError: x"),
            ("tests/t.py::test_refund_pays_a_rounding_case", "failed", "AE: x"),
        )
        measured = tc.measure_coverage(contract.coverage, run, tc.EXECUTED)
        self.assertFalse(measured.covered)
        self.assertEqual(
            tc.StrengthRefusal.REQUIREMENT_UNCOVERED.value, measured.refusal
        )
        self.assertIn("negative", measured.reason)

    def test_a_contract_without_a_negative_obligation_does_not_parse(self):
        """The refusal above is the runtime's; this is the model's.

        Both exist on purpose. A contract that never names a negative case is
        unrepresentable, so the runtime refusal only ever fires for a
        candidate that failed to write one.
        """
        with self.assertRaises(ValueError) as caught:
            pm.TestStrength(
                coverage=(obligation("R1", "positive", "test_pays"),),
                falsifiability=pm.Falsifiability(
                    strategy="baseline_absent",
                    expected_failing_selector="test_pays",
                    expected_reason_pattern="x",
                ),
            )
        self.assertIn("negative", str(caught.exception))

    def test_a_skipped_case_discharges_nothing(self):
        contract = strength()
        run = outcomes(
            ("tests/t.py::test_refund_pays_the_balance", "failed", "AssertionError"),
            ("tests/t.py::test_refund_rejects_a_negative_amount", "skipped", ""),
        )
        measured = tc.measure_coverage(contract.coverage, run, tc.EXECUTED)
        self.assertFalse(measured.covered)
        self.assertEqual(
            tc.StrengthRefusal.OBLIGATION_ONLY_SKIPPED.value, measured.refusal
        )

    def test_an_errored_case_discharges_nothing(self):
        contract = strength()
        run = outcomes(
            ("tests/t.py::test_refund_pays_the_balance", "failed", "AssertionError"),
            ("tests/t.py::test_refund_rejects_a_negative_amount", "errored", "boom"),
        )
        measured = tc.measure_coverage(contract.coverage, run, tc.EXECUTED)
        self.assertFalse(measured.covered)
        self.assertEqual(tc.StrengthRefusal.OBLIGATION_UNMET.value, measured.refusal)

    def test_both_obligations_executed_is_covered(self):
        contract = strength()
        run = outcomes(
            ("tests/t.py::test_refund_pays_the_balance", "failed", "AssertionError"),
            ("tests/t.py::test_refund_rejects_a_negative_amount", "failed", "AE"),
        )
        self.assertTrue(
            tc.measure_coverage(contract.coverage, run, tc.EXECUTED).covered
        )


# ── requirement 2: a planted name is not a case ─────────────────────────────


class APlantedSelectorIsRefused(unittest.TestCase):
    def test_an_obligation_matching_no_collected_case_is_refused(self):
        """The plan names the cases; the candidate must actually have them.

        This is the direction that catches a suite whose file name and node
        ids were written to look right. A selector that matches nothing is
        `REQUIREMENT_UNCOVERED` rather than a silent zero.
        """
        contract = strength()
        run = outcomes(
            ("tests/t.py::test_refund_pays_the_balance", "failed", "AssertionError"),
            ("tests/t.py::test_something_else", "failed", "AssertionError"),
        )
        measured = tc.measure_coverage(contract.coverage, run, tc.EXECUTED)
        self.assertFalse(measured.covered)
        self.assertEqual(
            tc.StrengthRefusal.REQUIREMENT_UNCOVERED.value, measured.refusal
        )
        self.assertIn("test_refund_rejects_a_negative_amount", measured.reason)

    def test_a_mutation_over_the_testers_own_files_is_refused_at_admission(self):
        """Reverting the test file is red because the cases are gone."""
        mapping = _pair_mapping(
            mutation={"kind": "revert_paths", "paths": ["tests/test_refund.py"]}
        )
        plan = pm.parse_mapping(mapping)
        blockers = pv._test_strength_coherent(plan)
        self.assertTrue(blockers)
        self.assertEqual(
            pv.Obligation.TEST_STRENGTH_COHERENT, blockers[0].obligation
        )
        self.assertIn("its own test file", blockers[0].message)

    def test_a_mutation_over_a_path_the_pair_does_not_produce_is_refused(self):
        mapping = _pair_mapping(
            mutation={"kind": "revert_paths", "paths": ["unrelated/other.py"]}
        )
        plan = pm.parse_mapping(mapping)
        blockers = pv._test_strength_coherent(plan)
        self.assertTrue(blockers)
        self.assertIn("not an output of the build node", blockers[0].message)

    def test_a_mutation_over_the_builders_outputs_is_admitted(self):
        mapping = _pair_mapping(
            mutation={"kind": "revert_paths", "paths": ["refunds.py"]}
        )
        plan = pm.parse_mapping(mapping)
        self.assertEqual([], pv._test_strength_coherent(plan))


# ── requirements 3 and 4: the negative control ──────────────────────────────


class TheNegativeControlIsExecuted(unittest.TestCase):
    def test_a_case_that_survives_the_control_is_refused(self):
        contract = strength()
        run = outcomes(
            ("tests/t.py::test_refund_pays_the_balance", "failed", "AssertionError"),
            ("tests/t.py::test_refund_rejects_it", "passed", ""),
        )
        result = tc.adjudicate_negative_control(contract.falsifiability, run)
        self.assertFalse(result.proven)
        self.assertEqual(tc.StrengthRefusal.CONTROL_NOT_RED.value, result.refusal)
        self.assertIn("test_refund_rejects_it", result.reason)

    def test_an_import_crash_is_not_falsifiability_proof(self):
        """Requirement 4. A tree that does not import is not a suite that
        discriminates, and counting it as one is how a broken fixture reads
        as a passing negative control."""
        contract = strength()
        run = outcomes(
            ("tests/t.py::test_refund_pays", "errored", "ModuleNotFoundError: yaml"),
        )
        result = tc.adjudicate_negative_control(contract.falsifiability, run)
        self.assertFalse(result.proven)
        self.assertEqual(tc.StrengthRefusal.CONTROL_IMPORT_CRASH.value, result.refusal)

    def test_a_run_with_no_report_is_not_proof(self):
        contract = strength()
        run = tc.CaseRun(outcomes=(), exit_code=4, collection_failed=True)
        result = tc.adjudicate_negative_control(contract.falsifiability, run)
        self.assertEqual(
            tc.StrengthRefusal.CONTROL_COLLECTION_FAILED.value, result.refusal
        )

    def test_a_selector_matching_no_case_is_unfalsifiable(self):
        contract = strength(failing_selector="test_nothing_named_this")
        run = outcomes(("tests/t.py::test_refund_pays", "failed", "AssertionError"))
        result = tc.adjudicate_negative_control(contract.falsifiability, run)
        self.assertEqual(
            tc.StrengthRefusal.CONTROL_NOT_SELECTED.value, result.refusal
        )

    def test_red_for_the_wrong_reason_is_refused(self):
        contract = strength(reason=r"refund must be positive")
        run = outcomes(
            ("tests/t.py::test_refund_pays", "failed", "TypeError: NoneType"),
        )
        result = tc.adjudicate_negative_control(contract.falsifiability, run)
        self.assertFalse(result.proven)
        self.assertEqual(
            tc.StrengthRefusal.CONTROL_WRONG_REASON.value, result.refusal
        )
        self.assertIn("TypeError: NoneType", result.reason)

    def test_red_for_the_declared_reason_is_proof(self):
        contract = strength(reason=r"refund must be positive")
        run = outcomes(
            ("tests/t.py::test_refund_pays", "failed",
             "AssertionError: refund must be positive"),
        )
        result = tc.adjudicate_negative_control(contract.falsifiability, run)
        self.assertTrue(result.proven, result.reason)
        self.assertEqual(
            ("tests/t.py::test_refund_pays",), result.failed_for_expected_reason
        )

    def test_no_declared_strategy_fails_closed(self):
        class Undeclared:
            strategy = ""
            expected_failing_selector = "x"
            expected_reason_pattern = "x"
            mutation = None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
            result = tc.execute_negative_control(
                falsifiability=Undeclared(),
                runner=tc.PytestCaseRunner(),
                repo=root,
                tree=root,
                candidate_sha="0" * 40,
                base_commit="0" * 40,
                nodeids=(),
            )
        self.assertFalse(result.proven)
        self.assertEqual(
            tc.StrengthRefusal.CONTROL_UNEXECUTABLE.value, result.refusal
        )


# ── requirement 13: one invariant, two runners ──────────────────────────────


class BothRunnersMeetOneInvariant(unittest.TestCase):
    """The adjudicators are pure functions over `CaseRun`, so a vitest node and
    a pytest node cannot drift into two definitions of `covered` or of `failed
    for the expected reason`. What differs is only how a `CaseRun` is built,
    which is what these parse assertions pin.
    """

    def test_the_two_runners_resolve_and_a_third_is_refused(self):
        self.assertEqual("pytest", tc.case_runner("pytest").name)
        self.assertEqual("vitest", tc.case_runner("vitest").name)
        with self.assertRaises(tc.RunnerUnsupported):
            tc.case_runner("jest")

    def test_pytest_short_summary_yields_per_case_outcomes(self):
        output = "\n".join(
            [
                "FAILED tests/t.py::test_a - AssertionError: refund must be positive",
                "SKIPPED tests/t.py::test_c - needs network",
                "1 failed, 1 passed, 1 skipped in 0.10s",
            ]
        )
        parsed = tc.parse_pytest_outcomes(
            output,
            ("tests/t.py::test_a", "tests/t.py::test_b", "tests/t.py::test_c"),
        )
        by_id = {item.nodeid: item for item in parsed}
        self.assertEqual("failed", by_id["tests/t.py::test_a"].status)
        self.assertIn("refund must be positive", by_id["tests/t.py::test_a"].reason)
        # A case the summary does not name passed: `-rf` reports only the
        # exceptional ones, so green is proven by having been asked for and
        # not reported, never by an absent line alone.
        self.assertEqual("passed", by_id["tests/t.py::test_b"].status)
        self.assertEqual("skipped", by_id["tests/t.py::test_c"].status)

    def test_a_pytest_file_error_binds_to_every_case_in_that_file(self):
        output = "\n".join(
            ["ERROR tests/t.py - ImportError: no module named refunds",
             "1 error in 0.02s"]
        )
        parsed = tc.parse_pytest_outcomes(
            output, ("tests/t.py::test_a", "tests/t.py::test_b")
        )
        self.assertEqual({"errored"}, {item.status for item in parsed})

    def test_vitest_json_yields_per_case_outcomes(self):
        report = json.dumps(
            {
                "testResults": [
                    {
                        "name": "/repo/tests/refund.test.ts",
                        "assertionResults": [
                            {"fullName": "refund pays the balance",
                             "status": "passed"},
                            {"fullName": "refund rejects a negative amount",
                             "status": "failed",
                             "failureMessages": [
                                 "AssertionError: refund must be positive\n at x"]},
                        ],
                    }
                ]
            }
        )
        parsed, ok = tc.parse_vitest_report("vitest banner\n" + report)
        self.assertTrue(ok)
        by_id = {item.nodeid: item for item in parsed}
        failed = by_id["/repo/tests/refund.test.ts::refund rejects a negative amount"]
        self.assertEqual("failed", failed.status)
        self.assertIn("refund must be positive", failed.reason)

    def test_vitest_with_no_report_is_a_collection_failure_not_a_green_run(self):
        parsed, ok = tc.parse_vitest_report("Error: cannot find vitest")
        self.assertFalse(ok)
        self.assertEqual((), parsed)

    def test_one_adjudicator_serves_both_runners(self):
        """Same contract, same verdicts, from either runner's node ids."""
        contract = strength(
            coverage=(
                obligation("R1", "positive", "pays the balance"),
                obligation("R1", "negative", "rejects a negative amount"),
            ),
            failing_selector="rejects a negative amount",
            reason="refund must be positive",
        )
        vitest_run = outcomes(
            ("t.test.ts::refund pays the balance", "failed", "AssertionError: x"),
            ("t.test.ts::refund rejects a negative amount", "failed",
             "AssertionError: refund must be positive"),
        )
        self.assertTrue(
            tc.measure_coverage(contract.coverage, vitest_run, tc.EXECUTED).covered
        )
        self.assertTrue(
            tc.adjudicate_negative_control(contract.falsifiability, vitest_run).proven
        )


# ── the plan schema, and what v3 keeps ──────────────────────────────────────


def _pair_mapping(mutation=None, version=pm.SCHEMA_V4, with_strength=True):
    gate = {
        "runner": "pytest",
        "argv": ["tests/test_refund.py"],
        "cwd": ".",
        "min_cases": 1,
    }
    tests_node = {
        "kind": "tests",
        "node_id": "tests",
        "instruction": "Write the refund tests.",
        "outputs": ["tests/test_refund.py"],
        "gate": gate,
    }
    if with_strength:
        tests_node["test_strength"] = {
            "coverage": [
                {"requirement_id": "R1", "aspect": "positive",
                 "case_selector": "test_refund_pays", "min_cases": 1},
                {"requirement_id": "R1", "aspect": "negative",
                 "case_selector": "test_refund_rejects", "min_cases": 1},
            ],
            "falsifiability": {
                "strategy": "controlled_mutation" if mutation else "baseline_absent",
                "mutation": mutation,
                "expected_failing_selector": "test_refund",
                "expected_reason_pattern": "AssertionError",
            },
        }
    return {
        "schema_version": version,
        "plan_id": "split",
        "repo": "fixture",
        "base_commit": "0" * 40,
        "intent": "split tests from build",
        "evidence": (),
        "nodes": [
            tests_node,
            {
                "kind": "agent",
                "node_id": "build",
                "needs": ["tests"],
                "instruction": "Implement refund.",
                "outputs": ["refunds.py"],
                "gate": gate,
            },
        ],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {
                "runner": "pytest",
                "argv": ["tests"],
                "cwd": ".",
                "min_cases": 1,
            },
        },
    }


class ThePlanSchemaCarriesTheContract(unittest.TestCase):
    def test_a_v4_tests_node_projects_its_contract_onto_the_plan_node(self):
        plan = pm.parse_mapping(_pair_mapping())
        node = next(n for n in plan.to_plan_nodes() if n.node_id == "tests")
        self.assertIsNotNone(node.test_strength)
        self.assertEqual(("R1",), node.test_strength.requirement_ids)

    def test_v3_stays_frozen_and_cannot_carry_the_field(self):
        """§6.3. A v4 tests node must not parse as a v3 one, or the version
        string would stop meaning 'authored knowing it must discriminate'."""
        mapping = _pair_mapping(version=pm.SCHEMA_V3)
        with self.assertRaises(pm.PlanParseError):
            pm.parse_mapping(mapping)

    def test_a_v3_plan_still_parses_and_projects_without_a_contract(self):
        plan = pm.parse_mapping(
            _pair_mapping(version=pm.SCHEMA_V3, with_strength=False)
        )
        node = next(n for n in plan.to_plan_nodes() if n.node_id == "tests")
        self.assertIsNone(node.test_strength)

    def test_a_contract_on_a_non_tests_node_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            st.PlanNode(
                node_id="build",
                kind=st.NodeKind.AGENT,
                depth=0,
                instruction="build",
                gate_command=("pytest",),
                gate_selector="tests",
                test_strength=strength(),
            )
        self.assertIn("tests node", str(caught.exception))

    def test_an_unparseable_expected_reason_is_a_plan_that_does_not_parse(self):
        with self.assertRaises(ValueError):
            pm.Falsifiability(
                strategy="baseline_absent",
                expected_failing_selector="x",
                expected_reason_pattern="(unclosed",
            )

    def test_a_mutation_strategy_without_a_mutation_is_refused(self):
        with self.assertRaises(ValueError):
            pm.Falsifiability(
                strategy="controlled_mutation",
                expected_failing_selector="x",
                expected_reason_pattern="x",
            )


class TheRunContractIsDerivedAndPinned(unittest.TestCase):
    def test_a_plan_whose_tests_nodes_declare_contracts_is_strength_v1(self):
        plan = pm.parse_mapping(_pair_mapping())
        self.assertIs(
            st.TestStrengthContract.STRENGTH_V1,
            sch.derive_test_strength_contract(plan.to_plan_nodes()),
        )

    def test_a_plan_whose_tests_nodes_declare_none_is_legacy(self):
        plan = pm.parse_mapping(
            _pair_mapping(version=pm.SCHEMA_V3, with_strength=False)
        )
        self.assertIs(
            st.TestStrengthContract.LEGACY,
            sch.derive_test_strength_contract(plan.to_plan_nodes()),
        )

    def test_a_plan_with_no_tests_nodes_is_strength_v1(self):
        node = st.PlanNode(
            node_id="build",
            kind=st.NodeKind.AGENT,
            depth=0,
            instruction="build",
            gate_command=("pytest",),
            gate_selector="tests",
        )
        self.assertIs(
            st.TestStrengthContract.STRENGTH_V1,
            sch.derive_test_strength_contract((node,)),
        )

    def test_a_mixed_node_set_has_no_answer_and_says_so(self):
        contracted = st.PlanNode(
            node_id="a", kind=st.NodeKind.TESTS, depth=0,
            outputs=("tests/test_a.py",), instruction="tests",
            gate_command=("pytest",), gate_selector="tests/test_a.py",
            test_strength=strength(),
        )
        bare = replace(contracted, node_id="b", test_strength=None)
        with self.assertRaises(sch.MixedTestStrengthContract):
            sch.derive_test_strength_contract((contracted, bare))


# ── the ledger: exactly-once evidence, and the pairing ──────────────────────


class TheEvidenceLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "lifecycle.db"
        self.store = lc.LifecycleStore(self.db)
        self.addCleanup(self.store.conn.close)
        self.tests = st.PlanNode(
            node_id="tests", kind=st.NodeKind.TESTS, depth=0,
            outputs=("tests/test_refund.py",), instruction="write tests",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )
        self.build = st.PlanNode(
            node_id="build", kind=st.NodeKind.AGENT, depth=1, needs=("tests",),
            outputs=("refunds.py",), instruction="implement",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )
        self.store.create_run(
            "run1", "d" * 64, [self.tests, self.build],
            test_strength_contract=st.TestStrengthContract.STRENGTH_V1,
        )

    def test_the_contract_pin_is_written_once_and_read_back(self):
        self.assertIs(
            st.TestStrengthContract.STRENGTH_V1,
            self.store.test_strength_contract("run1"),
        )

    def test_a_ledger_with_no_pin_reads_as_legacy(self):
        self.store.conn.execute(
            "UPDATE runs SET test_strength_contract=NULL WHERE run_id=?", ("run1",)
        )
        self.assertIs(
            st.TestStrengthContract.LEGACY,
            self.store.test_strength_contract("run1"),
        )

    def test_the_same_bytes_cannot_be_measured_to_two_answers(self):
        self.store.record_test_gate_evidence(
            "run1", "tests", "a" * 40, runner="pytest", selector="tests",
            strong=True, refusal=None, evidence={"strong": True},
        )
        with self.assertRaises(lc.LifecycleError):
            self.store.record_test_gate_evidence(
                "run1", "tests", "a" * 40, runner="pytest", selector="tests",
                strong=False, refusal="TEST_STRENGTH_CONTROL_NOT_RED",
                evidence={"strong": False},
            )

    def test_recording_the_identical_measurement_twice_is_a_replay(self):
        first = self.store.record_test_gate_evidence(
            "run1", "tests", "a" * 40, runner="pytest", selector="tests",
            strong=True, refusal=None, evidence={"strong": True},
        )
        second = self.store.record_test_gate_evidence(
            "run1", "tests", "a" * 40, runner="pytest", selector="tests",
            strong=True, refusal=None, evidence={"strong": True},
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)

    def test_strength_and_its_refusal_are_one_fact(self):
        with self.assertRaises(lc.LifecycleError):
            self.store.record_test_gate_evidence(
                "run1", "tests", "a" * 40, runner="pytest", selector="tests",
                strong=True, refusal="SOMETHING", evidence={},
            )
        with self.assertRaises(lc.LifecycleError):
            self.store.record_test_gate_evidence(
                "run1", "tests", "b" * 40, runner="pytest", selector="tests",
                strong=False, refusal=None, evidence={},
            )

    def test_acceptance_needs_both_strong_evidence_and_a_passed_review(self):
        self.store.record_test_gate_evidence(
            "run1", "tests", "a" * 40, runner="pytest", selector="tests",
            strong=True, refusal=None, evidence={"strong": True},
        )
        self.assertIsNone(self.store.accepted_test_candidate("run1", "tests"))
        self.store.ensure_derived_review_node("run1", "tests", depth=1)
        self.store.publish_candidate(
            "run1", "tests", "a" * 40, builder_generation=1
        )
        self.store.begin_review(
            "run1", "tests::review", "a" * 40, reviewer_generation=1
        )
        self.store.mark_review_dispatched(
            "run1", "tests::review", "a" * 40, reviewer_generation=1
        )
        self.store.complete_review(
            "run1", "tests::review", "a" * 40,
            verdict=st.ReviewVerdict.PASS,
            review_digest="e" * 64,
            receipt_path="/tmp/receipt.json",
            findings=(),
            reviewer_generation=1,
        )
        accepted = self.store.accepted_test_candidate("run1", "tests")
        self.assertIsNotNone(accepted)
        self.assertEqual("a" * 40, accepted.candidate_sha)

    def test_a_rejected_review_is_not_an_acceptance(self):
        self.store.record_test_gate_evidence(
            "run1", "tests", "a" * 40, runner="pytest", selector="tests",
            strong=True, refusal=None, evidence={"strong": True},
        )
        self.store.ensure_derived_review_node("run1", "tests", depth=1)
        self.store.publish_candidate(
            "run1", "tests", "a" * 40, builder_generation=1
        )
        self.store.begin_review(
            "run1", "tests::review", "a" * 40, reviewer_generation=1
        )
        self.store.mark_review_dispatched(
            "run1", "tests::review", "a" * 40, reviewer_generation=1
        )
        self.store.reject_and_create_handoff(
            "run1", "tests::review", "a" * 40,
            reviewer_generation=1,
            builder_generation=1,
            review_digest="e" * 64,
            receipt_path="/tmp/receipt.json",
            findings=({"check_id": "c", "object_id": "o", "message": "m"},),
        )
        self.assertIsNone(self.store.accepted_test_candidate("run1", "tests"))

    def test_a_pairing_cannot_be_rebound_to_other_test_bytes(self):
        """Requirement 6, at the ledger. An implementation already bound to
        one accepted candidate cannot be rebound to another, which is what a
        substituted test tree would need in order to inherit acceptance."""
        self.store.record_test_pairing(
            "run1", "build", "tests",
            accepted_test_sha="a" * 40, implementation_sha="b" * 40,
            verifier_command="pytest tests", selector="tests",
            executed_cases=2, coverage={},
        )
        with self.assertRaises(lc.LifecycleError):
            self.store.record_test_pairing(
                "run1", "build", "tests",
                accepted_test_sha="c" * 40, implementation_sha="b" * 40,
                verifier_command="pytest tests", selector="tests",
                executed_cases=2, coverage={},
            )


# ── requirement 11: the rollout, and what it refuses to do ──────────────────


class TheRolloutPreservesPinnedRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "lifecycle.db"
        self.store = lc.LifecycleStore(self.db)
        self.addCleanup(self.store.conn.close)
        tests = st.PlanNode(
            node_id="tests", kind=st.NodeKind.TESTS, depth=0,
            outputs=("tests/test_refund.py",), instruction="write tests",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )
        build = st.PlanNode(
            node_id="build", kind=st.NodeKind.AGENT, depth=1, needs=("tests",),
            outputs=("refunds.py",), instruction="implement",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )
        # No pin: a run created before the contract existed.
        self.store.create_run("run1", "d" * 64, [tests, build])
        self.store.conn.execute(
            "UPDATE node_lifecycle SET state=?, output_sha=? WHERE node_id=?",
            (st.NodeState.MERGED.value, "a" * 40, "tests"),
        )

    def test_a_legacy_merged_tests_node_is_classified_not_reopened(self):
        findings = self.store.legacy_test_strength_audit("run1")
        by_node = {item.tests_node_id: item for item in findings}
        self.assertEqual(
            lc.LEGACY_TEST_STRENGTH_UNPROVEN, by_node["tests"].classification
        )
        # The classification is informational: the node is still MERGED.
        self.assertEqual(st.NodeState.MERGED.value, by_node["tests"].state)
        self.assertFalse(by_node["tests"].blocking)
        row = self.store.conn.execute(
            "SELECT state FROM node_lifecycle WHERE node_id=?", ("tests",)
        ).fetchone()
        self.assertEqual(st.NodeState.MERGED.value, row[0])

    def test_the_audit_writes_nothing(self):
        before = self.store.conn.execute(
            "SELECT count(*) FROM legacy_test_strength_blocks"
        ).fetchone()[0]
        self.store.legacy_test_strength_audit("run1")
        after = self.store.conn.execute(
            "SELECT count(*) FROM legacy_test_strength_blocks"
        ).fetchone()[0]
        self.assertEqual(before, after)

    def test_a_dry_run_names_every_affected_node_and_changes_nothing(self):
        report = self.store.migrate_test_strength(
            "run1", apply=False, policy="block_unadmitted"
        )
        self.assertFalse(report.applied)
        self.assertIsNone(report.backup_path)
        self.assertEqual(("build",), report.blocked_nodes)
        self.assertEqual(
            0,
            self.store.conn.execute(
                "SELECT count(*) FROM legacy_test_strength_blocks"
            ).fetchone()[0],
        )

    def test_the_classify_policy_fences_nothing(self):
        report = self.store.migrate_test_strength(
            "run1", apply=True, policy="classify"
        )
        self.assertTrue(report.applied)
        self.assertEqual((), report.migrated_nodes)
        self.assertEqual((), self.store.legacy_test_strength_blocks("run1"))

    def test_apply_takes_a_backup_and_names_it(self):
        report = self.store.migrate_test_strength(
            "run1", apply=True, policy="block_unadmitted"
        )
        self.assertTrue(report.applied)
        self.assertIsNotNone(report.backup_path)
        self.assertTrue(Path(report.backup_path).is_file())
        self.assertEqual(("tests",), report.migrated_nodes)

    def test_an_already_admitted_dependant_is_never_fenced(self):
        """The invariant's sharpest edge. A dependant that ever ran was
        admitted under the pinned contract, and reaching back through it is
        exactly what the rollout forbids."""
        self.store.conn.execute(
            "UPDATE node_lifecycle SET state=? WHERE node_id=?",
            (st.NodeState.RUNNING.value, "build"),
        )
        report = self.store.migrate_test_strength(
            "run1", apply=False, policy="block_unadmitted"
        )
        self.assertEqual((), report.blocked_nodes)

    def test_migration_never_moves_a_terminal_row(self):
        self.store.migrate_test_strength(
            "run1", apply=True, policy="block_unadmitted"
        )
        row = self.store.conn.execute(
            "SELECT state, output_sha FROM node_lifecycle WHERE node_id=?",
            ("tests",),
        ).fetchone()
        self.assertEqual((st.NodeState.MERGED.value, "a" * 40), tuple(row))

    def test_an_unknown_policy_is_refused(self):
        with self.assertRaises(lc.LifecycleError):
            self.store.migrate_test_strength("run1", apply=True, policy="whatever")


# ── requirement 12: the operator surfaces ───────────────────────────────────


class TheSurfacesDistinguishAcceptanceFromMerge(unittest.TestCase):
    def test_a_private_test_acceptance_is_not_called_merged(self):
        phase = st.test_strength_phase(
            st.NodeKind.TESTS,
            st.NodeState.VERIFIED,
            st.LanePhase.ACCEPTED,
            accepted=True,
        )
        self.assertIs(st.TestStrengthPhase.TEST_ACCEPTED, phase)

    def test_an_authored_but_unreviewed_candidate_is_not_accepted(self):
        phase = st.test_strength_phase(
            st.NodeKind.TESTS,
            st.NodeState.VERIFIED,
            st.LanePhase.REVIEWING,
            accepted=False,
        )
        self.assertIs(st.TestStrengthPhase.TEST_REVIEWING, phase)

    def test_a_rejected_candidate_reads_as_rejected(self):
        self.assertIs(
            st.TestStrengthPhase.TEST_REJECTED,
            st.test_strength_phase(
                st.NodeKind.TESTS,
                st.NodeState.RUNNING,
                st.LanePhase.REPAIR_HANDOFF,
            ),
        )

    def test_an_implementation_merged_without_a_pairing_is_not_paired_merged(self):
        self.assertIs(
            st.TestStrengthPhase.IMPLEMENTATION_ACCEPTED,
            st.test_strength_phase(
                st.NodeKind.AGENT,
                st.NodeState.MERGED,
                st.LanePhase.ACCEPTED,
                paired=False,
            ),
        )

    def test_an_implementation_merged_with_its_pairing_is_paired_merged(self):
        self.assertIs(
            st.TestStrengthPhase.PAIRED_MERGED,
            st.test_strength_phase(
                st.NodeKind.AGENT,
                st.NodeState.MERGED,
                st.LanePhase.ACCEPTED,
                paired=True,
            ),
        )

    def test_a_code_node_has_no_phase_rather_than_an_invented_one(self):
        self.assertIsNone(
            st.test_strength_phase(
                st.NodeKind.CODE, st.NodeState.MERGED, None
            )
        )


# ── the reviewer's contract ─────────────────────────────────────────────────


class TheTestReviewerIsGivenItsContract(unittest.TestCase):
    def test_a_tests_node_is_judged_by_the_tests_rubric(self):
        self.assertIs(cr.TESTS_RUBRIC, cr.rubric_for(st.NodeKind.TESTS))
        self.assertIs(cr.CODE_RUBRIC, cr.rubric_for(st.NodeKind.AGENT))

    def test_the_two_rubrics_have_different_versions(self):
        """The rubric version is a component of the review digest, so a tests
        node and a build node cannot share a cached verdict."""
        self.assertNotEqual(cr.TESTS_RUBRIC.version, cr.CODE_RUBRIC.version)

    def test_a_kind_with_no_rubric_is_refused_not_defaulted(self):
        with self.assertRaises(cr.RubricUnavailable):
            cr.rubric_for(st.NodeKind.REVIEW)

    def test_the_handoff_carries_the_contract_and_the_measurements(self):
        node = st.PlanNode(
            node_id="tests", kind=st.NodeKind.TESTS, depth=0,
            outputs=("tests/test_refund.py",), instruction="write tests",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
            test_strength=strength(),
        )
        rendered = _render_handoff(node, test_evidence={
            "coverage": {
                "obligations": [
                    {"case_selector": "test_refund_pays_the_balance",
                     "selected": ["tests/t.py::test_refund_pays_the_balance"]},
                ]
            },
            "falsifiability": {
                "strategy": "baseline_absent",
                "selected": ["tests/t.py::test_refund_pays_the_balance"],
                "observed_reasons": ["AssertionError: refund must be positive"],
            },
        })
        self.assertIn("What these tests were required to prove", rendered)
        self.assertIn("R1 / negative", rendered)
        self.assertIn("The negative control that was executed", rendered)
        self.assertIn("AssertionError: refund must be positive", rendered)

    def test_a_contract_without_measurements_starves_the_reviewer(self):
        node = st.PlanNode(
            node_id="tests", kind=st.NodeKind.TESTS, depth=0,
            outputs=("tests/test_refund.py",), instruction="write tests",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
            test_strength=strength(),
        )
        with self.assertRaises(cr.HandoffIncomplete):
            _render_handoff(node, test_evidence=None)

    def test_a_build_node_handoff_renders_no_test_strength_block(self):
        node = st.PlanNode(
            node_id="build", kind=st.NodeKind.AGENT, depth=0,
            outputs=("refunds.py",), instruction="implement",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )
        rendered = _render_handoff(node, test_evidence=None)
        self.assertNotIn("What these tests were required to prove", rendered)


def _render_handoff(node, test_evidence):
    from adw_modules import finalization as fin

    rubric = cr.rubric_for(node.kind)
    objects = cr.review_objects(("tests/test_refund.py",), "b" * 40)
    matrix = fin.compute_matrix(rubric, "d" * 64, objects)
    handoff = cr.build_handoff(
        subject_digest="d" * 64,
        run_id="run1",
        node=node,
        base_sha="a" * 40,
        output_sha="b" * 40,
        diff="--- a\n+++ b\n",
        matrix=matrix,
        rubric=rubric,
        report_path=Path("/tmp/report.json"),
        test_evidence=test_evidence,
    )
    return handoff.render()


# ── requirements 5, 6, 7, 8, 9, 10: end to end, over a real repository ──────


GENUINE = (
    "def test_refund_pays_the_balance():\n"
    "    from refunds import refund\n"
    "    assert refund(600) == 100\n"
    "\n"
    "def test_refund_rejects_a_negative_amount():\n"
    "    from refunds import refund\n"
    "    try:\n"
    "        refund(-1)\n"
    "    except ValueError as exc:\n"
    "        assert 'refund must be positive' in str(exc)\n"
    "    else:\n"
    "        raise AssertionError('refund must be positive')\n"
)

WEAK = (
    "def test_refund_pays_the_balance():\n"
    "    from refunds import refund\n"
    "    assert refund(600) == 100\n"
)

IMPLEMENTATION = (
    "def refund(amount):\n"
    "    if amount < 0:\n"
    "        raise ValueError('refund must be positive')\n"
    "    return 100\n"
)

WEAK_IMPLEMENTATION = "def refund(amount):\n    return 100\n"

#: What the tester writes after its reviewer rejects `GENUINE` for covering
#: no boundary. A repair that adds a case, which is what a real correction
#: does and what the tests chain's "at least one new case" clause requires of
#: every candidate including a repaired one.
GENUINE_PLUS = GENUINE + (
    "\n"
    "def test_refund_rejects_zero_at_the_boundary():\n"
    "    from refunds import refund\n"
    "    try:\n"
    "        refund(-0.01)\n"
    "    except ValueError as exc:\n"
    "        assert 'refund must be positive' in str(exc)\n"
    "    else:\n"
    "        raise AssertionError('refund must be positive')\n"
)


class RejectingReview:
    def __init__(self, candidate_sha):
        self.passed = False
        self.subject_digest = "fixture-review-" + candidate_sha
        self.findings = ({"check_id": "c", "object_id": "o", "message": "weak"},)
        self.advisories = ()
        self.unreachable = ()


class GateStrengthEndToEnd(SchedulerFixture):
    """The whole chain, over a real git repository and real pytest runs."""

    def head(self):
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def tests_node(self, contract=None):
        return st.PlanNode(
            node_id="tests",
            kind=st.NodeKind.TESTS,
            depth=0,
            outputs=("tests/test_refund.py",),
            instruction="Write the refund tests.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
            test_strength=contract if contract is not None else strength(
                coverage=(
                    obligation("R1", "positive", "test_refund_pays_the_balance"),
                    obligation(
                        "R1", "negative", "test_refund_rejects_a_negative_amount"
                    ),
                ),
                reason=r"(AssertionError|ModuleNotFoundError|refund must be positive)",
            ),
        )

    def build_node(self):
        return st.PlanNode(
            node_id="build",
            kind=st.NodeKind.AGENT,
            depth=1,
            needs=("tests",),
            outputs=("refunds.py",),
            instruction="Implement refund.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )

    def strength_deps(self, **kw):
        base = dict(plan_base_commit=self.head())
        base.update(kw)
        return self.deps(**base)

    def evidence(self, node_id="tests"):
        return self.store.test_gate_evidence("run1", node_id)

    def test_a_weak_candidate_is_refused_and_never_reaches_a_reviewer(self):
        """Requirement 1, end to end: the missing negative case is the
        refusal, and it happens before a reviewer's turn is spent."""
        self.written["tests"] = {"tests/test_refund.py": WEAK}
        reviewed = []

        def review(attempt, node, record, base_sha, candidate_sha, _resume):
            reviewed.append(node.node_id)
            return _Passing(candidate_sha)

        self.schedule(
            [self.tests_node()], deps=self.strength_deps(review_attempt=review)
        ).run()
        self.assertNotEqual(st.NodeState.MERGED.value, self.states()["tests"])
        self.assertNotIn("tests", reviewed)
        recorded = self.evidence()
        self.assertTrue(recorded)
        self.assertFalse(recorded[-1].strong)
        self.assertEqual(
            tc.StrengthRefusal.REQUIREMENT_UNCOVERED.value, recorded[-1].refusal
        )

    def test_a_strong_candidate_records_evidence_and_is_reviewed(self):
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        reviewed = []

        def review(attempt, node, record, base_sha, candidate_sha, _resume):
            reviewed.append((node.node_id, candidate_sha))
            return _Passing(candidate_sha)

        self.schedule(
            [self.tests_node()], deps=self.strength_deps(review_attempt=review)
        ).run()
        self.assertEqual(st.NodeState.MERGED.value, self.states()["tests"])
        recorded = self.evidence()
        self.assertTrue(recorded[-1].strong, recorded[-1].refusal)
        self.assertEqual(["tests"], [node_id for node_id, _ in reviewed])
        accepted = self.store.accepted_test_candidate("run1", "tests")
        self.assertIsNotNone(accepted)
        self.assertEqual(recorded[-1].candidate_sha, accepted.candidate_sha)

    def test_a_rejected_test_candidate_does_not_merge(self):
        """Requirement 8's precondition: the reviewer's verdict is what
        decides, and a rejected candidate is not an accepted one."""
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.schedule(
            [self.tests_node()],
            config=self.config(review_ceiling=1, test_review_ceiling=1),
            deps=self.strength_deps(
                review_attempt=lambda *a: RejectingReview(a[4])
            ),
        ).run()
        self.assertNotEqual(st.NodeState.MERGED.value, self.states()["tests"])
        self.assertIsNone(self.store.accepted_test_candidate("run1", "tests"))

    def test_a_test_review_rejection_spends_its_own_budget(self):
        """Requirement: the test-review budget is distinct from every other."""
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.schedule(
            [self.tests_node()],
            config=self.config(review_ceiling=1, test_review_ceiling=1),
            deps=self.strength_deps(
                review_attempt=lambda *a: RejectingReview(a[4])
            ),
        ).run()
        spends = self.store.lane_retry_spends("run1", "tests", limit=100)
        self.assertTrue(spends)
        self.assertIn(
            st.LaneRetryClass.TEST_REVIEW_REJECTION,
            {item.retry_class for item in spends},
        )

    def test_the_implementation_is_bound_to_the_exact_accepted_test_bytes(self):
        """Requirements 5 and 10."""
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.written["build"] = {"refunds.py": IMPLEMENTATION}
        self.gate_script[("build", "pre")] = [red()]
        self.gate_script[("build", "post")] = [green()]
        self.gate_script[("build", "falsify")] = [red()]
        self.schedule(
            [self.tests_node(), self.build_node()], deps=self.strength_deps()
        ).run()
        states = self.states()
        self.assertEqual(st.NodeState.MERGED.value, states["tests"])
        self.assertEqual(st.NodeState.MERGED.value, states["build"])
        pairings = self.store.test_pairings("run1", "build")
        self.assertEqual(1, len(pairings))
        accepted = self.store.accepted_test_candidate("run1", "tests")
        self.assertEqual(accepted.candidate_sha, pairings[0].accepted_test_sha)
        self.assertGreaterEqual(pairings[0].executed_cases, 2)

    def test_an_implementation_that_edits_the_accepted_tests_cannot_merge(self):
        """Requirement 6. Substituted test bytes cannot inherit acceptance."""
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.written["build"] = {
            "refunds.py": WEAK_IMPLEMENTATION,
            "tests/test_refund.py": WEAK,
        }
        self.gate_script[("build", "pre")] = [red()]
        self.gate_script[("build", "post")] = [green()]
        self.gate_script[("build", "falsify")] = [red()]
        self.schedule(
            [self.tests_node(), self.build_node()], deps=self.strength_deps()
        ).run()
        self.assertEqual(st.NodeState.MERGED.value, self.states()["tests"])
        self.assertNotEqual(st.NodeState.MERGED.value, self.states()["build"])
        self.assertEqual((), self.store.test_pairings("run1", "build"))

    def test_an_implementation_that_fails_the_accepted_tests_cannot_merge(self):
        """Requirement 5 from the other side: a green *node* gate is not the
        accepted candidate's coverage obligations going green."""
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.written["build"] = {"refunds.py": WEAK_IMPLEMENTATION}
        self.gate_script[("build", "pre")] = [red()]
        self.gate_script[("build", "post")] = [green()]
        self.gate_script[("build", "falsify")] = [red()]
        self.schedule(
            [self.tests_node(), self.build_node()], deps=self.strength_deps()
        ).run()
        self.assertNotEqual(st.NodeState.MERGED.value, self.states()["build"])
        blob = self._transition_detail("build")
        self.assertIn(tc.PairingRefusal.GATE_NOT_GREEN.value, blob)

    def test_a_completed_test_review_is_not_redispatched_after_resume(self):
        """Requirement 7."""
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        dispatches = []

        def review(attempt, node, record, base_sha, candidate_sha, _resume):
            dispatches.append(candidate_sha)
            return _Passing(candidate_sha)

        self.schedule(
            [self.tests_node()], deps=self.strength_deps(review_attempt=review)
        ).run()
        first = len(dispatches)
        self.assertEqual(1, first)
        # A second scheduler over the same ledger: the review is terminal, so
        # the reviewer is not asked again and no fresh attempt is created to
        # escape recovery.
        self.schedule(
            [self.tests_node()], deps=self.strength_deps(review_attempt=review)
        ).run()
        self.assertEqual(first, len(dispatches))

    def test_the_measurement_is_not_repeated_for_the_same_candidate(self):
        """Requirement 9's ledger half: the same immutable bytes carry one
        measurement, so a stale callback cannot install a second answer."""
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.schedule([self.tests_node()], deps=self.strength_deps()).run()
        recorded = self.evidence()
        # One row per immutable candidate, not per measurement attempt.
        self.assertEqual(
            len({item.candidate_sha for item in recorded}), len(recorded)
        )
        with self.assertRaises(lc.LifecycleError):
            self.store.record_test_gate_evidence(
                "run1", "tests", recorded[0].candidate_sha,
                runner="pytest", selector="tests/test_refund.py",
                strong=False, refusal="TEST_STRENGTH_CONTROL_NOT_RED",
                evidence={},
            )

    def test_a_legacy_contract_run_keeps_the_old_acceptance(self):
        """The rollout invariant, over the scheduler. A plan whose tests node
        declares no contract runs under LEGACY: the strength gate does not
        fire, and the node merges exactly as it did before."""
        legacy = replace(self.tests_node(), test_strength=None)
        self.written["tests"] = {"tests/test_refund.py": WEAK}
        scheduler = self.schedule([legacy], deps=self.strength_deps())
        self.assertIs(
            st.TestStrengthContract.LEGACY, scheduler.test_strength_contract
        )
        scheduler.run()
        self.assertEqual(st.NodeState.MERGED.value, self.states()["tests"])
        self.assertEqual((), self.evidence())

    def _transition_detail(self, node_id):
        rows = self.store.conn.execute(
            "SELECT detail_json FROM transitions WHERE node_id=?", (node_id,)
        ).fetchall()
        return " ".join(row[0] or "" for row in rows)

    def states(self):
        return {
            record.node_id: record.state
            for record in self.store.node_records("run1")
        }


class _Passing:
    def __init__(self, candidate_sha):
        self.passed = True
        self.subject_digest = "fixture-review-" + candidate_sha
        self.findings = ()
        self.advisories = ()
        self.unreachable = ()


if __name__ == "__main__":
    unittest.main()


# ── the deterministic smoke fixture ─────────────────────────────────────────


class ScriptedReview:
    """A reviewer whose verdict per candidate is decided by the fixture."""

    def __init__(self, candidate_sha, passed):
        self.passed = passed
        self.subject_digest = "fixture-review-" + candidate_sha
        self.findings = (
            ()
            if passed
            else ({"check_id": "tests.exercises_real_boundaries",
                   "object_id": "tests/test_refund.py",
                   "message": "asserts nothing the contract names"},)
        )
        self.advisories = ()
        self.unreachable = ()


class DeterministicSmoke(SchedulerFixture):
    """One run, end to end, through every state the contract adds.

    Weak tests rejected, corrected tests accepted, a weak implementation
    refused against the accepted tests, the corrected implementation paired
    and merged — with a scheduler process replaced on both sides of test
    acceptance, and with the review dispatch count and the attempt count both
    asserted, because "recovered" and "started over" are indistinguishable
    from the outside and only the second one is a bug.
    """

    def setUp(self):
        super().setUp()
        self.scripts = {}
        self.review_dispatches = []
        self.review_verdicts = {}

    def head(self):
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    # The fixture's agent: each attempt writes the next scripted tree, and the
    # last one repeats, so a retry that should not have happened is visible as
    # an exhausted script rather than as a silently identical candidate.
    def run_node(self, attempt, node, record, retry_prompt, on_launch, cancel):
        self.prompts.setdefault(node.node_id, []).append(retry_prompt)
        on_launch(None)
        script = self.scripts.get(node.node_id)
        if script:
            self.written[node.node_id] = script.pop(0) if len(script) > 1 else script[0]
        return super().run_node(attempt, node, record, retry_prompt, lambda _p: None,
                                cancel)

    def review_attempt(self, attempt, node, record, base_sha, candidate_sha, _resume):
        self.review_dispatches.append((node.node_id, candidate_sha))
        verdicts = self.review_verdicts.setdefault(node.node_id, [])
        passed = verdicts.pop(0) if len(verdicts) > 1 else (
            verdicts[0] if verdicts else True)
        return ScriptedReview(candidate_sha, passed)

    def nodes(self):
        tests = st.PlanNode(
            node_id="tests", kind=st.NodeKind.TESTS, depth=0,
            outputs=("tests/test_refund.py",),
            instruction="Write the refund tests.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
            test_strength=strength(
                coverage=(
                    obligation("R1", "positive", "test_refund_pays_the_balance"),
                    obligation(
                        "R1", "negative", "test_refund_rejects_a_negative_amount"
                    ),
                ),
                reason=r"(AssertionError|ModuleNotFoundError|refund must be positive)",
            ),
        )
        build = st.PlanNode(
            node_id="build", kind=st.NodeKind.AGENT, depth=1, needs=("tests",),
            outputs=("refunds.py",), instruction="Implement refund.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )
        return [tests, build]

    def states(self):
        return {
            record.node_id: record.state
            for record in self.store.node_records("run1")
        }

    def test_the_whole_lifecycle_converges_on_an_exact_paired_merge(self):
        self.scripts["tests"] = [
            {"tests/test_refund.py": WEAK},        # refused: no negative case
            {"tests/test_refund.py": GENUINE},     # rejected by the reviewer
            {"tests/test_refund.py": GENUINE_PLUS},  # repaired, then accepted
        ]
        self.scripts["build"] = [
            {"refunds.py": WEAK_IMPLEMENTATION},  # refused against the tests
            {"refunds.py": IMPLEMENTATION},       # accepted
        ]
        self.review_verdicts["tests"] = [False, True]
        self.review_verdicts["build"] = [True]
        self.gate_script[("build", "pre")] = [red(), red(), red()]
        self.gate_script[("build", "post")] = [green(), green(), green()]
        self.gate_script[("build", "falsify")] = [red(), red(), red()]

        deps = self.deps(plan_base_commit=self.head())

        # First process: stopped after the tests node's first refusal, which
        # is the crash *before* test acceptance.
        first = self.schedule(self.nodes(), deps=deps)
        first.run()

        # Second process over the same ledger: recovery, not a restart.
        second = self.schedule(self.nodes(), deps=deps)
        second.run()

        states = self.states()
        self.assertEqual(st.NodeState.MERGED.value, states["tests"], states)
        self.assertEqual(st.NodeState.MERGED.value, states["build"], states)

        # The weak candidate was measured and refused, and the refusal is
        # durable rather than a line in a log.
        evidence = self.store.test_gate_evidence("run1", "tests")
        refusals = [item.refusal for item in evidence if not item.strong]
        self.assertIn(
            tc.StrengthRefusal.REQUIREMENT_UNCOVERED.value, refusals
        )
        accepted = self.store.accepted_test_candidate("run1", "tests")
        self.assertIsNotNone(accepted)

        # Exactly one review per immutable candidate: a resumed run that
        # redispatched a completed review would show a repeat here.
        self.assertEqual(
            len(set(self.review_dispatches)), len(self.review_dispatches)
        )

        # The rejected test candidate went back to the retained tester's
        # generation rather than to a fresh actor.
        handoffs = self.store.repair_handoffs("run1", "tests", limit=100)
        self.assertTrue(handoffs)
        self.assertEqual(
            {st.RepairHandoffState.ACKNOWLEDGED},
            {handoff.state for handoff in handoffs},
        )

        # The merge is the exact accepted pair, and nothing else.
        pairings = self.store.test_pairings("run1", "build")
        self.assertEqual(1, len(pairings))
        self.assertEqual(accepted.candidate_sha, pairings[0].accepted_test_sha)
        merged_build = self.store.get_node("run1", "build")
        self.assertEqual(
            merged_build.output_sha, pairings[0].implementation_sha
        )


class TheContractPinDoesNotStrandExistingRuns(SchedulerFixture):
    """Resuming a run created before the pin existed must still work.

    The pin is compared at projection so a run cannot change contracts under
    itself. Compared unconditionally, it strands every run that already
    exists: a plan with no tests nodes derives `STRENGTH_V1` — there is
    nothing weaker about it — while a ledger written before the column reads
    `LEGACY`, and the two would disagree about a contract that governs nothing
    in that run.
    """

    def build_only(self):
        return st.PlanNode(
            node_id="build", kind=st.NodeKind.AGENT, depth=0,
            outputs=("refunds.py",), instruction="Implement refund.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )

    def _tests_pair(self):
        tests = st.PlanNode(
            node_id="tests", kind=st.NodeKind.TESTS, depth=0,
            outputs=("tests/test_refund.py",), instruction="Write tests.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
            test_strength=strength(),
        )
        return [tests]

    def unpin(self):
        self.store.conn.execute(
            "UPDATE runs SET test_strength_contract=NULL WHERE run_id=?",
            ("run1",),
        )

    def test_a_build_only_run_created_before_the_pin_still_projects(self):
        self.schedule([self.build_only()], deps=self.deps()).project()
        self.unpin()
        self.assertIs(
            st.TestStrengthContract.LEGACY,
            self.store.test_strength_contract("run1"),
        )
        # No raise: the contract governs nothing in a run with no tests nodes.
        self.schedule([self.build_only()], deps=self.deps()).project()

    def test_a_tests_run_whose_pin_disagrees_is_refused(self):
        self.schedule(self._tests_pair(), deps=self.deps()).project()
        self.assertIs(
            st.TestStrengthContract.STRENGTH_V1,
            self.store.test_strength_contract("run1"),
        )
        self.unpin()
        with self.assertRaises(sch.TestStrengthContractMismatch):
            self.schedule(self._tests_pair(), deps=self.deps()).project()

    def test_a_legacy_tests_run_resumes_under_its_own_contract(self):
        legacy = [replace(node, test_strength=None)
                  for node in self._tests_pair()]
        self.schedule(legacy, deps=self.deps()).project()
        self.unpin()
        # LEGACY pin, LEGACY plan: the run resumes under the rules it was
        # created with, which is the whole invariant.
        self.schedule(legacy, deps=self.deps()).project()


class RunStartRefusesAnUncontractedTestsPlan(unittest.TestCase):
    """The other half of the rollout invariant.

    An existing run keeps the contract it was created under — that is what
    `TheRolloutPreservesPinnedRuns` above asserts. This is the side that makes
    "the new lifecycle is mandatory for newly created runs" true rather than
    aspirational: a *new* run cannot be created under the old rules.
    """

    def _plan(self, **kw):
        import maestro  # noqa: F401  (imported here; the module is heavy)

        return pm.parse_mapping(_pair_mapping(**kw))

    def _refuse(self, plan):
        import maestro

        args = type("Args", (), {"plan_file": "plan.json"})()
        try:
            maestro._refuse_uncontracted_tests_nodes(args, plan)
        except maestro._RunRefused as refusal:
            return refusal
        return None

    def test_a_contracted_plan_is_admitted(self):
        self.assertIsNone(self._refuse(self._plan()))

    def test_a_v3_tests_plan_is_refused_by_name(self):
        plan = self._plan(version=pm.SCHEMA_V3, with_strength=False)
        refusal = self._refuse(plan)
        self.assertIsNotNone(refusal)
        self.assertEqual(
            "RUN_TEST_STRENGTH_CONTRACT_ABSENT", refusal.outcome)

    def test_the_refusal_carries_the_nodes_as_a_typed_field(self):
        """§1.2 — a fact a caller must branch on travels as a field. An
        operator tool deciding which plans to re-ship reads these, not the
        prose."""
        plan = self._plan(version=pm.SCHEMA_V3, with_strength=False)
        refusal = self._refuse(plan)
        self.assertEqual(
            ["tests"], refusal.fields["uncontracted_tests_nodes"])
        self.assertEqual(
            pm.SCHEMA_V4, refusal.fields["required_schema_version"])

    def test_a_plan_with_no_tests_nodes_is_admitted(self):
        mapping = _pair_mapping()
        mapping["nodes"] = [
            node for node in mapping["nodes"] if node["kind"] != "tests"]
        mapping["nodes"][0]["needs"] = []
        mapping["schema_version"] = pm.SCHEMA_V2
        self.assertIsNone(self._refuse(pm.parse_mapping(mapping)))
