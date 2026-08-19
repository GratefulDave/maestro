"""The integration gate is asked questions that are answerable about it.

A node's gate and the plan's integration gate are different objects with
opposite scope rules. A node gate is scoped to that node's own work (§7.4);
the integration gate is the one gate in the design that runs the whole suite,
at the final head where every merged node's work is present at once (§8.8).
Asking the node-scoped questions of the integration gate is a category error,
and `gate.selector_is_scoped_to_this_node` is BLOCKING, so the category error
was terminal for the bytes: a plan whose integration gate legitimately spans
tests an earlier plan merged plus tests this run's lanes will write (§19 M14)
failed finalization on a cell whose correct answer the architecture states.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import finalization as fin
from adw_modules import plan_model as pm
from adw_modules.plan_finalization import review_objects

NODE_SCOPED_CHECKS = ("gate.asserts_a_post_condition",
                      "gate.selector_is_scoped_to_this_node")


def _checks(kind: fin.ObjectKind) -> set:
    obj = fin.ReviewObject(object_id="o", kind=kind)
    return {c.check_id for c in fin.DEFAULT_RUBRIC.applicable(obj)}


class IntegrationGateRubricTest(unittest.TestCase):
    def plan(self) -> pm.Plan:
        return pm.Plan(
            schema_version="maestro-plan.v1", plan_id="p", repo=".",
            base_commit="abc", intent="ship",
            evidence=(pm.Hypothesis(kind="hypothesis", evidence_id="h1",
                                    statement="risk"),),
            nodes=(
                pm.AgentNode(
                    kind="agent", node_id="build", instruction="build",
                    outputs=("src/a.py",),
                    gate=pm.Gate(runner="pytest",
                                 argv=("pytest", "tests/test_a.py"),
                                 cwd=".", min_cases=1),
                ),
            ),
            merge_policy=pm.MergePolicy(
                integration_branch="maestro/p",
                integration_gate=pm.Gate(runner="pytest",
                                         argv=("pytest", "tests/"),
                                         cwd=".", min_cases=1),
            ),
        )

    def test_integration_gate_is_not_asked_the_node_scoped_questions(self):
        """§8.8: this gate runs the whole suite, so a selector naming only
        one node's outputs would be the defect rather than the standard, and
        no node's red-before state is a fact about it at all."""
        applied = _checks(fin.ObjectKind.INTEGRATION_GATE)
        for check_id in NODE_SCOPED_CHECKS:
            self.assertNotIn(check_id, applied)

    def test_integration_gate_is_asked_about_coverage_and_min_cases(self):
        applied = fin.DEFAULT_RUBRIC.applicable(
            fin.ReviewObject(object_id="plan#integration-gate",
                             kind=fin.ObjectKind.INTEGRATION_GATE))
        by_id = {c.check_id: c for c in applied}
        self.assertIn("gate.selector_covers_the_merged_surface", by_id)
        self.assertEqual(by_id["gate.selector_covers_the_merged_surface"].severity,
                         fin.Severity.BLOCKING)
        self.assertIn("gate.min_cases_is_meaningful", by_id)
        # The kind is not a synonym for GATE with a relabelled question set:
        # every check it carries must declare it.
        for check in applied:
            self.assertIn(fin.ObjectKind.INTEGRATION_GATE, check.applies_to)

    def test_node_gates_keep_exactly_the_checks_they_had(self):
        self.assertEqual(_checks(fin.ObjectKind.GATE), {
            "gate.asserts_a_post_condition",
            "gate.selector_is_scoped_to_this_node",
            "gate.min_cases_is_meaningful",
        })

    def test_coverage_check_is_not_asked_of_a_node_gate(self):
        self.assertNotIn("gate.selector_covers_the_merged_surface",
                         _checks(fin.ObjectKind.GATE))

    def test_projection_types_the_two_gates_apart(self):
        objects = review_objects(self.plan())
        kinds = {obj.object_id: obj.kind for obj in objects}
        self.assertEqual(kinds["node:build#gate"], fin.ObjectKind.GATE)
        self.assertEqual(kinds["plan#integration-gate"],
                         fin.ObjectKind.INTEGRATION_GATE)

    def test_rubric_version_records_the_applicability_change(self):
        """The matrix a receipt persists is only interpretable against the
        rubric that produced it, so a changed applicability matrix is a new
        rubric version."""
        self.assertEqual(fin.DEFAULT_RUBRIC.version, "maestro-rubric.v2")

    def test_every_projected_kind_carries_at_least_one_check(self):
        """B15's rule turned on the rubric itself: a kind the projection emits
        that no check declares produces an object nobody is asked anything
        about, which is silent loss of coverage rather than a failure. This is
        the guard for the next kind added, not for the two gates."""
        for obj in review_objects(self.plan()):
            with self.subTest(object_id=obj.object_id):
                self.assertTrue(fin.DEFAULT_RUBRIC.applicable(obj),
                                "{} ({}) has no applicable check".format(
                                    obj.object_id, obj.kind.value))

    def test_matrix_asks_the_integration_gate_only_its_own_checks(self):
        matrix = fin.compute_matrix(fin.DEFAULT_RUBRIC, "d" * 64,
                                    review_objects(self.plan()))
        asked = {cell.check_id for cell in matrix.graded_cells
                 if cell.object_id == "plan#integration-gate"}
        self.assertEqual(asked, {"gate.selector_covers_the_merged_surface",
                                 "gate.min_cases_is_meaningful"})


if __name__ == "__main__":
    unittest.main()
