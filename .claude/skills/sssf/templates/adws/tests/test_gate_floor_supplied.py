"""§6.4's seventeenth obligation — a gate's `min_cases` must be supplied.

The defect this convicts, measured rather than reasoned. In
`run-8d1a71f463e4430f92a125a8f8b3731d`, `lane-routing-chemical`'s tests node
authored and merged `tests/epa_build/test_routing_chemical.py` defining
exactly **two** test functions with no parametrisation. The build lane paired
to it declared `min_executed: 5` over that same selector, and
`compare_test_bytes` requires the builder to carry the accepted test bytes
verbatim — so the collectable count over that selector was fixed at two
before any builder started, and the gate was unsatisfiable by construction.
Four builders were dispatched against it in sequence. All four forged the
gate rather than fail, escalating from two forgery sites to fourteen.

Every step was individually correct. `_gate_executable`'s produced arm
deliberately exempts a selector the plan is about to create, because a
collection count there would necessarily be zero. What nobody asked was the
other question: not *how many cases exist at base* but **how many cases this
plan guarantees will exist**, against the threshold a sibling will be judged
on.

That number is decidable at authoring time, and it is decidable in the
direction that matters — a *lower* bound. Two facts supply it:

* `verify_tests_node` refuses a tests candidate with `new_case_count < 1`
  (`NO_NEW_CASES`), so **every** accepted tests node contributes at least one
  case, contract or no contract. That is the floor for a `maestro-plan.v3`
  tests node, which declares nothing else;
* a `maestro-plan.v4` tests node's `CoverageObligation.min_cases` is measured
  by `measure_coverage` over distinct case node ids, so a single obligation
  demanding *n* cases guarantees *n* distinct executed cases. Obligations may
  select overlapping cases — `case_selector` is a substring match, and one
  case id can contain two selectors — so the guaranteed distinct total is the
  **maximum** over obligations, never the sum.

Ceiling questions stay undecidable and are not asked: nothing stops a tester
writing more cases than its contract obligates, so this refuses only what the
plan fails to *guarantee*, and says so in those words.

Run with:  uv run adws/adw_test.py -k gate_floor_supplied
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import plan_validate as pv  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=str(repo),
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError("git {0}: {1}".format(" ".join(args),
                                                   result.stderr))
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    """A repository holding nothing the pair produces.

    Deliberately bare: both the tests node's file and the build node's module
    are absent at base, which is what puts this plan's gates on
    `_gate_executable`'s produced arm — the arm that exempts them from a
    collection count and therefore from every existing check.
    """
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))
    (repo / "README.md").write_text("fixture repository\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


TEST_PATH = "tests/epa_build/test_routing_chemical.py"


def coverage(min_cases: int = 1):
    """The two obligations `maestro-plan.v4` requires of one requirement.

    `min_cases` defaults to 1, which is the shape every shipped contract
    carries and the shape that guarantees one case, not five.
    """
    return [
        {"requirement_id": "R1", "aspect": "positive",
         "case_selector": "test_routes_chemical", "min_cases": min_cases},
        {"requirement_id": "R1", "aspect": "negative",
         "case_selector": "test_rejects_unrouted", "min_cases": min_cases},
    ]


def pair_mapping(base_commit: str, *, gate_min_cases: int,
                 build_gate_min_cases: int = None,
                 obligation_min_cases: int = 1,
                 version: str = pm.SCHEMA_V4) -> dict:
    """`lane-routing-chemical`'s shape: one tests node, one paired builder.

    The two share a gate core, which `_tests_build_may_share_gate` permits and
    which is the pair's contract — the tests node writes the cases, the build
    node makes them pass. Both are therefore judged on `gate_min_cases` over a
    selector whose entire case supply the tests node fixes.
    """
    if build_gate_min_cases is None:
        build_gate_min_cases = gate_min_cases
    gate = {
        "runner": "pytest",
        "argv": [TEST_PATH],
        "cwd": ".",
        "min_cases": gate_min_cases,
    }
    build_gate = dict(gate, min_cases=build_gate_min_cases)
    tests_node = {
        "kind": "tests",
        "node_id": "lane-routing-chemical-tests",
        "instruction": "Write the chemical-routing tests.",
        "outputs": [TEST_PATH],
        "gate": dict(gate),
    }
    if version != pm.SCHEMA_V3:
        tests_node["test_strength"] = {
            "coverage": coverage(obligation_min_cases),
            "falsifiability": {
                "strategy": "baseline_absent",
                "mutation": None,
                "expected_failing_selector": "test_routes_chemical",
                "expected_reason_pattern": "ModuleNotFoundError|AssertionError",
            },
        }
    return {
        "schema_version": version,
        "plan_id": "epa-build",
        "repo": "example/repo",
        "base_commit": base_commit,
        "intent": "route chemical filings",
        "evidence": [],
        "nodes": [
            tests_node,
            {
                "kind": "agent",
                "node_id": "lane-routing-chemical",
                "needs": ["lane-routing-chemical-tests"],
                "instruction": "Implement chemical routing.",
                "outputs": ["epa_build/routing_chemical.py"],
                "gate": dict(build_gate),
            },
        ],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {"runner": "pytest", "cwd": ".",
                                 "argv": ["tests"], "min_cases": 1},
        },
        "supersedes": None,
    }


class Collector:
    """The gate-collector seam. Every selector in these plans is produced, so
    a real collector would answer zero for all of them; answering zero here
    makes that explicit rather than incidental."""

    def __init__(self, counts=None):
        self.counts = {"tests": 3}
        if counts:
            self.counts.update(counts)
        self.calls = []

    def collect(self, gate, tree):
        selector = pm.selector_of(gate)
        self.calls.append(selector)
        return self.counts.get(selector, 0)


class Receipts:
    def has_receipt(self, digest):
        return False


class FloorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.collector = Collector()
        self.receipts = Receipts()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def validate(self, mapping):
        stored = pc.canonicalize(pm.parse_mapping(mapping))
        return pv.validate_plan(stored, self.repo, receipts=self.receipts,
                                collector=self.collector)

    def assertBlocked(self, result, obligation):
        self.assertEqual(pv.Outcome.AUTHORING_BLOCKED, result.outcome)
        found = [b for b in result.blockers if b.obligation is obligation]
        self.assertTrue(found, "expected {0}, got {1}".format(
            obligation, [b.obligation for b in result.blockers]))
        for blocker in found:
            self.assertTrue(blocker.pointer.startswith("/"))
            self.assertTrue(blocker.message.strip())
        return found

    def assertNoBlocker(self, result, obligation):
        offending = [b for b in result.blockers if b.obligation is obligation]
        self.assertEqual([], offending,
                         "; ".join(b.message for b in offending))


class TheProductionCase(FloorTestCase):
    """The plan shape that burned a night, and its green control."""

    def test_a_builder_demanding_more_than_the_tester_is_held_to_is_refused(self):
        """`lane-routing-chemical`'s defect stated as a declaration mismatch:
        the tester is held to 2 cases, the builder is judged on 5, and the
        builder cannot close the gap because it carries the test bytes
        verbatim."""
        result = self.validate(
            pair_mapping(self.base, gate_min_cases=2, build_gate_min_cases=5))
        found = self.assertBlocked(result, pv.Obligation.GATE_FLOOR_SUPPLIED)
        messages = " ".join(b.message for b in found)
        self.assertIn("lane-routing-chemical", messages)
        self.assertIn("5", messages)

    def test_only_the_builder_half_is_refused_not_the_tester_s_own_gate(self):
        """The tests node's own gate is a number compared against itself, so
        it cannot fail here. Refusing it too would send an author to edit the
        one declaration that is already consistent."""
        result = self.validate(
            pair_mapping(self.base, gate_min_cases=2, build_gate_min_cases=5))
        found = self.assertBlocked(result, pv.Obligation.GATE_FLOOR_SUPPLIED)
        self.assertEqual({"/nodes/1/gate/min_cases"},
                         {b.pointer for b in found})

    def test_the_refusal_names_the_declaration_to_raise(self):
        """The author's instinct is to collapse the coverage obligations into
        one demanding five cases, which asserts something they do not mean.
        The message must send them to the tests node's own min_cases."""
        found = self.assertBlocked(
            self.validate(pair_mapping(self.base, gate_min_cases=2,
                                       build_gate_min_cases=5)),
            pv.Obligation.GATE_FLOOR_SUPPLIED)
        message = found[0].message
        self.assertIn("Raise the min_cases of "
                      "lane-routing-chemical-tests", message)
        self.assertIn("Do NOT collapse coverage obligations", message)
        self.assertIn("aspect, not a case budget", message)

    def test_raising_the_testers_own_declaration_makes_it_eligible(self):
        """The repair the message prescribes, executed."""
        result = self.validate(
            pair_mapping(self.base, gate_min_cases=5, build_gate_min_cases=5))
        self.assertNoBlocker(result, pv.Obligation.GATE_FLOOR_SUPPLIED)
        self.assertEqual(pv.Outcome.FINALIZATION_ELIGIBLE, result.outcome)

    def test_the_ordinary_single_case_pair_stays_eligible(self):
        result = self.validate(pair_mapping(self.base, gate_min_cases=1))
        self.assertNoBlocker(result, pv.Obligation.GATE_FLOOR_SUPPLIED)
        self.assertEqual(pv.Outcome.FINALIZATION_ELIGIBLE, result.outcome)


class TheCostOfTheSymmetricCase(FloorTestCase):
    """Documented weakening, asserted rather than described (2026-08-27).

    A symmetric pair cannot fail this obligation — the comparison is a number
    against itself. That is the price of deriving the floor from the tests
    node's own declaration instead of from its coverage obligations, and it
    is paid deliberately: the alternative forced authors to write a false
    single-obligation. The earliness moves to acceptance rather than
    vanishing, which the class below asserts against the same numbers.
    """

    def test_a_symmetric_pair_is_not_refused_however_thin_its_contract(self):
        """Seven aspects at one case each under a nine-case pair — the FDAdb
        shape. Eligible here, and correctly so: nothing in the plan is
        inconsistent. Whether nine cases actually arrive is measured later."""
        mapping = pair_mapping(self.base, gate_min_cases=9)
        mapping["nodes"][0]["test_strength"]["coverage"] = coverage(1)
        self.assertNoBlocker(self.validate(mapping),
                             pv.Obligation.GATE_FLOOR_SUPPLIED)

    def test_the_acceptance_arm_catches_what_this_one_now_lets_through(self):
        """The same nine, measured. A candidate collecting two cases against a
        nine-case declaration is refused before any builder is dispatched."""
        evidence = tc.GateStrengthEvidence(
            tests_node_id="lane-routing-chemical-tests",
            candidate_sha="a" * 40, runner="pytest", selector=TEST_PATH,
            contract_declared=True,
            executed_nodeids=(TEST_PATH + "::test_a", TEST_PATH + "::test_b"),
            coverage=tc.CoverageMeasurement(),
            falsifiability=tc.FalsifiabilityResult(proven=True),
            gate_min_cases=9)
        self.assertEqual(tc.StrengthRefusal.GATE_FLOOR_UNREACHABLE.value,
                         evidence.refusal)


class AnObligationAboveTheGateRaisesTheFloor(FloorTestCase):
    """`measure_coverage` requires that many selected-and-executed cases, and
    a selected case is a collected one — so an obligation demanding more than
    the gate is enforced too, and the floor is the maximum of the two."""

    def test_a_large_obligation_supplies_a_larger_builder_gate(self):
        result = self.validate(
            pair_mapping(self.base, gate_min_cases=2, build_gate_min_cases=6,
                         obligation_min_cases=6))
        self.assertNoBlocker(result, pv.Obligation.GATE_FLOOR_SUPPLIED)

    def test_but_obligations_are_never_summed(self):
        """Two one-case obligations do not make two cases. `case_selector` is
        a substring match, so one case id can contain both — which is what
        `fdadb-v2-wp6-geo-layer`'s English-phrase selectors make concrete
        rather than theoretical."""
        result = self.validate(
            pair_mapping(self.base, gate_min_cases=1, build_gate_min_cases=2,
                         obligation_min_cases=1))
        self.assertBlocked(result, pv.Obligation.GATE_FLOOR_SUPPLIED)


class AnUncontractedTestsNodeGuaranteesOne(FloorTestCase):
    """`maestro-plan.v3` declares no contract, so a LEGACY-pinned run never
    measures test strength and its declared `min_cases` is enforced by
    nothing. The only guarantee left is `NO_NEW_CASES`: one case."""

    def test_a_v3_pair_above_one_is_refused_however_it_declares_itself(self):
        result = self.validate(
            pair_mapping(self.base, gate_min_cases=5,
                         version=pm.SCHEMA_V3))
        found = self.assertBlocked(result, pv.Obligation.GATE_FLOOR_SUPPLIED)
        self.assertIn("enforced by nothing",
                      " ".join(b.message for b in found))

    def test_a_v3_pair_at_one_stays_eligible(self):
        result = self.validate(
            pair_mapping(self.base, gate_min_cases=1,
                         version=pm.SCHEMA_V3))
        self.assertNoBlocker(result, pv.Obligation.GATE_FLOOR_SUPPLIED)
        self.assertEqual(pv.Outcome.FINALIZATION_ELIGIBLE, result.outcome)


class WhatThisObligationDoesNotReach(FloorTestCase):
    """The bound is a supply the *plan* fixes. Where an agent node owns its
    own test file it can write as many cases as its gate demands, so there is
    no unsatisfiability to refuse and refusing anyway would break every
    unpaired plan in the repository."""

    def test_an_agent_node_writing_its_own_tests_is_not_refused(self):
        mapping = {
            "schema_version": pm.SCHEMA_V2,
            "plan_id": "solo",
            "repo": "example/repo",
            "base_commit": self.base,
            "intent": "write a greeting and cover it",
            "evidence": [],
            "nodes": [
                {"kind": "agent", "node_id": "n-write", "needs": [],
                 "outputs": ["src/greeting.py", "tests/test_greeting.py"],
                 "instruction": "write the greeting and its test",
                 "gate": {"runner": "pytest", "cwd": ".",
                          "argv": ["tests/test_greeting.py"],
                          "min_cases": 70}},
            ],
            "merge_policy": {
                "integration_branch": "main",
                "integration_gate": {"runner": "pytest", "cwd": ".",
                                     "argv": ["tests"], "min_cases": 1},
            },
            "supersedes": None,
        }
        self.assertNoBlocker(self.validate(mapping),
                             pv.Obligation.GATE_FLOOR_SUPPLIED)

    def test_a_selector_reaching_beyond_the_pair_is_not_refused(self):
        """A gate spanning the tests node's file *and* a path it does not own
        has a supply this plan does not fix, so no lower bound is derivable
        and no refusal is honest. `_gate_executable`'s mixed arm already
        holds that case."""
        mapping = pair_mapping(self.base, gate_min_cases=5)
        for node in mapping["nodes"]:
            node["gate"]["argv"] = [TEST_PATH, "tests/test_preexisting.py"]
        self.assertNoBlocker(self.validate(mapping),
                             pv.Obligation.GATE_FLOOR_SUPPLIED)


class TheObligationIsCountedAndNamed(FloorTestCase):
    def test_it_joins_the_enumerated_set(self):
        self.assertIn(pv.Obligation.GATE_FLOOR_SUPPLIED, pv.OBLIGATIONS)
        self.assertEqual(len(pv.OBLIGATIONS), len(set(pv.OBLIGATIONS)))
        self.assertEqual(set(pv.OBLIGATIONS), set(pv.Obligation))


# ── the acceptance arm: the same question, measured instead of derived ──────
#
# The authoring arm above refuses a gate the plan does not *guarantee*. This
# one refuses a gate the accepted bytes provably cannot reach. Both are kept
# because they answer at different strengths and at different moments: at
# authoring nothing has been written yet, so "not guaranteed" is the strongest
# honest claim; at tests-node acceptance the file exists and the count is
# exact, so the refusal is "impossible" rather than "unpromised".
#
# It is also the arm that survives a plan reaching a run without passing
# `validate_plan` — a hand-built node set, or a plan shipped before this
# obligation existed.


class TheAcceptanceArmMeasuresTheFrozenSupply(unittest.TestCase):
    """`tc.GateStrengthEvidence` compared against the node's own threshold."""

    def evidence(self, *, cases: int, floor=None):
        strong_halves = {
            "coverage": tc.CoverageMeasurement(),
            "falsifiability": tc.FalsifiabilityResult(
                strategy="baseline_absent", executed=True, proven=True),
        }
        kwargs = {} if floor is None else {"gate_min_cases": floor}
        return tc.GateStrengthEvidence(
            tests_node_id="lane-routing-chemical-tests",
            candidate_sha="a" * 40,
            runner="pytest",
            selector=TEST_PATH,
            contract_declared=True,
            executed_nodeids=tuple(
                "{0}::test_case_{1}".format(TEST_PATH, index)
                for index in range(cases)),
            **strong_halves, **kwargs)

    def test_two_collected_cases_under_a_five_case_floor_is_refused(self):
        """The production numbers, measured rather than derived."""
        evidence = self.evidence(cases=2, floor=5)
        self.assertEqual(2, evidence.collected_case_count)
        self.assertFalse(evidence.gate_floor_reachable)
        self.assertEqual(tc.StrengthRefusal.GATE_FLOOR_UNREACHABLE.value,
                         evidence.refusal)
        self.assertFalse(evidence.strong)
        verdict = tc.verify_test_strength(evidence)
        self.assertFalse(verdict.verified)
        self.assertIn("3 more case", verdict.reason)
        self.assertIn("lane-routing-chemical-tests", verdict.reason)

    def test_the_refusal_says_what_to_change_not_only_that_it_refused(self):
        reason = tc.verify_test_strength(self.evidence(cases=2, floor=5)).reason
        self.assertIn("freezes the count", reason)
        self.assertIn("verbatim", reason)
        self.assertIn("lower min_cases", reason)

    def test_a_supply_that_reaches_the_floor_is_accepted(self):
        evidence = self.evidence(cases=5, floor=5)
        self.assertIsNone(evidence.refusal)
        self.assertTrue(tc.verify_test_strength(evidence).verified)

    def test_a_repeated_node_id_is_counted_once(self):
        """A node id is unique per run under both runners, so a repeat can
        only be a measurement artefact. Counting it twice would report supply
        that does not exist — which is the shape a sibling agent got past a
        membership-only check five times over."""
        evidence = tc.GateStrengthEvidence(
            tests_node_id="t", candidate_sha="a" * 40, runner="pytest",
            selector=TEST_PATH, contract_declared=True,
            executed_nodeids=(TEST_PATH + "::test_x",) * 5,
            coverage=tc.CoverageMeasurement(),
            falsifiability=tc.FalsifiabilityResult(proven=True),
            gate_min_cases=5)
        self.assertEqual(1, evidence.collected_case_count)
        self.assertEqual(tc.StrengthRefusal.GATE_FLOOR_UNREACHABLE.value,
                         evidence.refusal)

    def test_the_floor_is_asked_last_so_a_weaker_defect_is_not_buried(self):
        """A candidate that fails coverage hears about coverage. Reporting a
        sibling's threshold first would hide the more actionable defect."""
        uncovered = tc.CoverageMeasurement(
            (), tc.StrengthRefusal.REQUIREMENT_UNCOVERED.value, "no case")
        evidence = tc.GateStrengthEvidence(
            tests_node_id="t", candidate_sha="a" * 40, runner="pytest",
            selector=TEST_PATH, contract_declared=True,
            executed_nodeids=(), coverage=uncovered,
            falsifiability=tc.FalsifiabilityResult(proven=True),
            gate_min_cases=5)
        self.assertEqual(tc.StrengthRefusal.REQUIREMENT_UNCOVERED.value,
                         evidence.refusal)

    def test_an_unstated_floor_compares_nothing_and_refuses_nothing(self):
        """The compatibility arm. A construction predating the field behaves
        exactly as it did — and says so in the ledger rather than silently."""
        evidence = self.evidence(cases=2)
        self.assertEqual(tc.UNSTATED_GATE_FLOOR, evidence.gate_min_cases)
        self.assertTrue(evidence.gate_floor_reachable)
        self.assertTrue(evidence.strong)

    def test_the_durable_row_carries_the_comparison_and_its_inputs(self):
        """§1.2 — the transition keys on a typed record, so the record has to
        hold every number the decision used, not just its outcome."""
        mapping = self.evidence(cases=2, floor=5).as_mapping()
        self.assertEqual(5, mapping["gate_min_cases"])
        self.assertEqual(2, mapping["collected_case_count"])
        self.assertFalse(mapping["gate_floor_reachable"])
        self.assertEqual(tc.StrengthRefusal.GATE_FLOOR_UNREACHABLE.value,
                         mapping["refusal"])
        self.assertFalse(mapping["strong"])


class TheThresholdReachesTheEvidence(unittest.TestCase):
    """A reader-without-writer sweep over `scheduler._prove_test_strength`.

    §7.4 records that this technique, and not a test of either end, is what
    caught `min_cases` being dropped by a field-by-field projection: "the
    source has a field, the destination does not, and neither the type
    checker nor any test comparing the two ends is looking at the field that
    did not survive."

    `gate_min_cases` defaults to `UNSTATED_GATE_FLOOR`, which is what lets it
    be added without breaking a construction that predates it — and is
    therefore exactly the shape §3.6 B8 warns about, a field that is optional
    forever unless something refuses its absence. This is that refusal. It
    reads the scheduler; it does not modify it.
    """

    def test_every_evidence_construction_states_the_gate_threshold(self):
        source = (ADWS / "adw_modules" / "scheduler.py").read_text()
        tree = ast.parse(source)
        missing = []
        found = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name != "GateStrengthEvidence":
                continue
            found += 1
            if not any(kw.arg == "gate_min_cases" for kw in node.keywords):
                missing.append(node.lineno)
        self.assertTrue(
            found, "no GateStrengthEvidence construction found in "
                   "scheduler.py; this guard has stopped guarding anything")
        self.assertEqual(
            [], missing,
            "scheduler.py builds GateStrengthEvidence at line(s) {0} without "
            "passing gate_min_cases, so the node's own threshold never "
            "reaches the acceptance check and every candidate is measured "
            "against UNSTATED_GATE_FLOOR. Pass "
            "gate_min_cases=node.gate_min_cases.".format(missing))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
