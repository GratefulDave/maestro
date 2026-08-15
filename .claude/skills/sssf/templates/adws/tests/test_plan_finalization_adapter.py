"""Step 2 plan projects into Step 3 review objects without digest recoupling."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import finalization as fin
from adw_modules import plan_model as pm
from adw_modules.plan_finalization import review_objects


class PlanFinalizationAdapterTest(unittest.TestCase):
    def plan(self) -> pm.Plan:
        return pm.Plan(
            schema_version="maestro-plan.v1", plan_id="p", repo=".",
            base_commit="abc", intent="ship",
            evidence=(pm.Hypothesis(kind="hypothesis", evidence_id="h1", statement="risk"),),
            nodes=(
                pm.AgentNode(
                    kind="agent", node_id="build", instruction="build", outputs=("src/a.py",),
                    gate=pm.Gate(runner="pytest", argv=("pytest", "tests/test_a.py"), cwd=".", min_cases=1),
                ),
                pm.CodeNode(kind="code", node_id="check", needs=("build",), command=("python", "-m", "compileall"), cwd=".", expects_changes=False),
            ),
            merge_policy=pm.MergePolicy(
                integration_branch="maestro/p",
                integration_gate=pm.Gate(runner="pytest", argv=("pytest",), cwd=".", min_cases=1),
            ),
        )

    def test_mapping_is_complete_stable_and_typed(self):
        objects = review_objects(self.plan())
        self.assertEqual(objects, (
            fin.ReviewObject("plan", fin.ObjectKind.PLAN),
            fin.ReviewObject("node:build", fin.ObjectKind.NODE),
            fin.ReviewObject("node:build#gate", fin.ObjectKind.GATE),
            fin.ReviewObject("node:check", fin.ObjectKind.NODE),
            fin.ReviewObject("plan#integration-gate", fin.ObjectKind.GATE),
            fin.ReviewObject("evidence:h1", fin.ObjectKind.EVIDENCE),
        ))

    def test_finalization_does_not_import_plan_canonical(self):
        source = Path(fin.__file__).read_text()
        imported = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("plan_canonical", imported)


if __name__ == "__main__":
    unittest.main()
