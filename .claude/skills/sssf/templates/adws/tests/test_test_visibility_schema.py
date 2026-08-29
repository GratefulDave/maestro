"""`maestro-plan.v5`'s `test_visibility`, and the migration it must not cause.

Two properties are settled here and they pull in opposite directions, which is
why they are tested together.

**v5 exists.** A tests node can declare `test_visibility`, it survives the
projection onto `scheduler_types.PlanNode`, and the invalid shapes are refused
by the constructor rather than downstream.

**v5 cannot start a run yet, and v4 still can.** The composed-evaluation-tree
machinery that a hidden node needs — out-of-band gates, the absence/provenance/
coverage conjuncts, the sanitised repair handoff — is not built. A hidden node
executed under today's scheduler would be gated by `pytest <path>` against a
worktree that does not contain the path, which is not a red for the intended
reason and would convict every honest attempt. So v5 is deliberately absent
from `_RUNNABLE_PLAN_SCHEMA_VERSIONS` and fails closed at `run start`.

The half-landed state is the point. #104 is the record of what happens when a
schema change reaches deployments before the runtime that honours it: shipped
plans became unrunnable mid-flight, repairable only by re-shipping from the IR.
Here the new version is inert and every previously runnable version is
untouched, so mirroring this template into a deployment changes nothing about
what that deployment can already run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from pydantic import ValidationError  # noqa: E402

import maestro  # noqa: E402
from adw_modules import plan_model  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def _tests_node(visibility: str | None) -> dict:
    node: dict = {
        "node_id": "lane-refund-tests",
        "kind": "tests",
        "needs": [],
        "outputs": ["tests/test_refund.py"],
        "instruction": "Write the cases that prove the refund rules.",
        "gate": {
            "runner": "pytest",
            "argv": ["tests/test_refund.py"],
            "cwd": ".",
            "min_cases": 2,
        },
        "test_strength": {
            "coverage": [
                {
                    "requirement_id": "R-refund-01",
                    "aspect": "positive",
                    "case_selector": "test_refund_pays_the_balance",
                    "min_cases": 1,
                },
                {
                    "requirement_id": "R-refund-01",
                    "aspect": "negative",
                    "case_selector": "test_refund_rejects_a_negative_amount",
                    "min_cases": 1,
                },
            ],
            "falsifiability": {
                "strategy": "baseline_absent",
                "mutation": None,
                "expected_failing_selector": "test_refund",
                "expected_reason_pattern": "refund must be positive",
            },
        },
    }
    if visibility is not None:
        node["test_visibility"] = visibility
    return node


class TheVersionIsRegisteredAndParses(unittest.TestCase):
    def test_a_v5_tests_node_must_declare_a_visibility(self):
        """Required, not defaulted — the whole reason it is a new version.

        A defaulted field could not distinguish an author who chose `merged`
        from an author who never heard of the choice (§3.6 B8).

        `ValidationError` rather than `PlanParseError` because this validates
        the node model directly; `PlanParseError` is what the plan-level parse
        wraps it in.
        """
        with self.assertRaises(ValidationError) as caught:
            plan_model.TestsNodeV5.model_validate(_tests_node(None))
        self.assertIn("test_visibility", str(caught.exception))

    def test_a_v4_tests_node_rejects_a_visibility_it_cannot_carry(self):
        """v4 forbids extras, so a v5 node cannot parse as a v4 one."""
        with self.assertRaises(Exception):
            plan_model.TestsNodeV4.model_validate(_tests_node("hidden"))


class TheProjectionCarriesIt(unittest.TestCase):
    def test_a_declared_visibility_reaches_the_scheduler_node(self):
        node = plan_model.TestsNodeV5.model_validate(_tests_node("hidden"))
        self.assertEqual(node.test_visibility, st.VISIBILITY_HIDDEN)

    def test_a_node_that_declares_none_projects_as_merged(self):
        """A v3/v4 node's absence of a visibility means what it always meant.

        The projection must not invent a decision the author never made — the
        shape §19 M26 convicts in another field.
        """
        projected = st.PlanNode(
            node_id="lane-refund-tests",
            kind=st.NodeKind.TESTS,
            depth=0,
            needs=(),
            outputs=("tests/test_refund.py",),
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
            gate_min_cases=2,
            instruction="Write the cases.",
        )
        self.assertEqual(projected.test_visibility, st.VISIBILITY_MERGED)


class InvalidShapesAreRefusedByTheConstructor(unittest.TestCase):
    """Refused where the value is built, not three modules downstream."""

    def _node(self, **overrides):
        fields = dict(
            node_id="n1",
            kind=st.NodeKind.TESTS,
            depth=0,
            needs=(),
            outputs=("tests/test_refund.py",),
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
            gate_min_cases=2,
            instruction="Write the cases.",
            test_strength=object(),
        )
        fields.update(overrides)
        return st.PlanNode(**fields)

    def test_an_unrecognised_visibility_is_refused(self):
        """Fail closed: an unknown value would read as 'not hidden'."""
        with self.assertRaises(ValueError) as caught:
            self._node(test_visibility="invisible")
        self.assertIn("not one of", str(caught.exception))

    def test_hidden_on_a_non_tests_kind_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self._node(
                kind=st.NodeKind.AGENT,
                test_strength=None,
                test_visibility=st.VISIBILITY_HIDDEN,
            )
        self.assertIn("only a tests node has visibility", str(caught.exception))

    def test_hidden_without_a_strength_contract_is_refused(self):
        """The coverage obligations are the sanitised handoff's only vocabulary."""
        with self.assertRaises(ValueError) as caught:
            self._node(test_strength=None, test_visibility=st.VISIBILITY_HIDDEN)
        self.assertIn("needs a test-strength contract", str(caught.exception))


class V5CannotStartARunYet(unittest.TestCase):
    """The deliberate half-landing, asserted so it cannot be undone by accident."""

    def test_v5_is_not_runnable_while_the_composed_gate_is_unbuilt(self):
        """Remove this test in the same change that builds the composed gate.

        A hidden node under today's scheduler would be gated by its declared
        `pytest <path>` inside a worktree that does not contain the path: a
        collection error, which §7.4 refuses as a red for the wrong reason, on
        every honest attempt.
        """
        self.assertNotIn(
            plan_model.SCHEMA_V5,
            maestro._RUNNABLE_PLAN_SCHEMA_VERSIONS,
            "v5 must fail closed at run start until hidden execution exists",
        )

    def test_every_previously_runnable_version_is_untouched(self):
        """Mirroring this template must not make a deployment's plans unrunnable."""
        self.assertEqual(
            maestro._RUNNABLE_PLAN_SCHEMA_VERSIONS,
            frozenset(
                {
                    plan_model.SCHEMA_V2,
                    plan_model.SCHEMA_V3,
                    plan_model.SCHEMA_V4,
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
