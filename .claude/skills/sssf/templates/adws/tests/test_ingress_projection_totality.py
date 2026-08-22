"""Executable proof that ingress totality is a raise, not a silent drop.

`plan_model._assert_projection_is_total` guards Plan → PlanNode.
`plan_contract_ingress._assert_ingress_projection_is_total` guards the
projection one step earlier: plan-contract.v1 → maestro-plan. A
lane-bound IR field with no destination used to vanish, and
ProjectionTotalityTest could not see it.

Issue #96 measured six instances. This file is the measured case
§16.3 requires: a field with neither a destination nor a named
exemption refuses, and each named absence keeps its destination or
its reason.

Run with: PYTHONPATH=. pytest tests/test_ingress_projection_totality.py -q
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, str(_path))

from adw_modules import plan_contract_ingress as pci  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402


P5 = TESTS / "fixtures" / "cmo-consolidation-l-r6-p5.plan.json"


def _ir() -> dict:
    """The smallest IR that projects. Mutations below are one edit to it."""
    return {
        "schema_version": "plan-contract.v1",
        "plan_id": "phase-1",
        "title": "Phase 1 freeze",
        "plan_kind": "brownfield",
        "source_artifacts": [{
            "source_id": "src-readme",
            "path": "README.md",
            "sha256": "a" * 64,
            "required": True,
        }],
        "requirements": [{
            "requirement_id": "req-freeze",
            "text": "Freeze the writers behind a greeting module.",
            "surface": [
                {"path": "src/greeting.py", "mutation": "written"},
                {"path": "README.md", "mutation": "unmodified"},
            ],
            "effects": [],
        }],
        "lanes": [{
            "lane_id": "lane-freeze",
            "title": "Freeze writers",
            "execution_context": ".",
            "requirement_ids": ["req-freeze"],
            "depends_on": [],
            "verifier_ids": ["verify-freeze"],
        }],
        "verifiers": [{
            "verifier_id": "verify-freeze",
            "lane_ids": ["lane-freeze"],
            "source_ids": ["src-readme"],
            "command": "python3 -m pytest tests/test_existing.py",
            "min_executed": 1,
        }],
        "extensions": {"maestro": {
            "repo": "example",
            "outputs": {"lane-freeze": ["src/greeting.py"]},
            "prohibited_effects": [],
            "integration_branch": "main",
            "integration_gate": {
                "runner": "pytest", "argv": ["tests"], "cwd": ".",
                "min_cases": 1,
            },
        }},
    }


class IngressFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def project(self, mutate=None) -> dict:
        ir = _ir()
        if mutate is not None:
            mutate(ir)
        return pci.project_draft(ir, self.repo)

    def node(self, mutate=None) -> dict:
        return self.project(mutate)["nodes"][0]


class IngressProjectionTotalityTest(IngressFixture):
    """§3.6 B15 at ingress — a field with no destination is a raise."""

    def test_the_shipped_projection_is_total(self):
        """The unmodified fixture still projects, and the guard ran."""
        node = self.node()
        self.assertEqual(node["node_id"], "lane-freeze")
        self.assertEqual(node["reads"], ["src-readme"])
        self.assertEqual(node["kind"], pci._EMITTED_NODE_KIND)

    def test_a_newly_declared_lane_field_with_no_home_raises(self):
        """The measured case: a field added later is refused, not dropped."""
        def mutate(ir: dict) -> None:
            ir["lanes"][0]["field_added_later"] = "unprojected"
        with self.assertRaises(pci.IngressProjectionIncomplete) as caught:
            self.project(mutate)
        self.assertIn("field_added_later", str(caught.exception))
        self.assertIn("_LANE_PROJECTION", str(caught.exception))

    def test_a_newly_declared_requirement_field_with_no_home_raises(self):
        def mutate(ir: dict) -> None:
            ir["requirements"][0]["contract_field_added_later"] = "x"
        with self.assertRaises(pci.IngressProjectionIncomplete) as caught:
            self.project(mutate)
        self.assertIn("contract_field_added_later", str(caught.exception))
        self.assertIn("_REQUIREMENT_PROJECTION", str(caught.exception))

    def test_a_newly_declared_verifier_field_with_no_home_raises(self):
        def mutate(ir: dict) -> None:
            ir["verifiers"][0]["threshold_added_later"] = 3
        with self.assertRaises(pci.IngressProjectionIncomplete) as caught:
            self.project(mutate)
        self.assertIn("threshold_added_later", str(caught.exception))
        self.assertIn("_VERIFIER_PROJECTION", str(caught.exception))

    def test_a_newly_declared_claim_field_with_no_home_raises(self):
        def mutate(ir: dict) -> None:
            ir["lanes"][0]["claim_ids"] = ["claim-freeze"]
            ir["claims"] = [{
                "claim_id": "claim-freeze",
                "predicate": "exercises",
                "field_added_later": "unprojected",
            }]
        with self.assertRaises(pci.IngressProjectionIncomplete) as caught:
            self.project(mutate)
        self.assertIn("field_added_later", str(caught.exception))
        self.assertIn("_CLAIM_PROJECTION", str(caught.exception))

    def test_a_field_carried_by_name_but_not_by_value_raises(self):
        """Stronger than a name check, which the source_ids drop would pass."""
        ir = _ir()
        draft = pci.project_draft(ir, self.repo)
        wrong = dict(draft["nodes"][0])
        wrong["reads"] = []
        with self.assertRaises(pci.IngressProjectionIncomplete) as caught:
            pci._assert_ingress_projection_is_total(
                ir, ir["lanes"][0], ir["verifiers"][0], wrong)
        self.assertIn("reads", str(caught.exception))

    def test_requirement_source_ids_reach_reads(self):
        """Instance 2: a requirement may not name evidence its node drops."""
        def mutate(ir: dict) -> None:
            ir["source_artifacts"].append({
                "source_id": "src-section-l-audit",
                "path": "docs/AUDIT.md",
                "sha256": "b" * 64,
                "required": True,
            })
            ir["requirements"][0]["source_ids"] = ["src-section-l-audit"]
        node = self.node(mutate)
        self.assertEqual(
            node["reads"], ["src-readme", "src-section-l-audit"])

    def test_requirement_source_ids_are_not_exempt(self):
        self.assertNotIn(
            "source_ids", pci._REQUIREMENT_PROJECTION_EXEMPT)
        self.assertEqual(
            pci._REQUIREMENT_PROJECTION["source_ids"], "reads")

    def test_each_named_absence_keeps_its_exemption_and_reason(self):
        """Issue #96: a test that fails if any of the six loses its reason.

        Instance 2 (requirements[].source_ids) is carried, not exempted.
        The other five stay dropped, each with a stated reason.
        """
        for name in ("claim_ids", "seam_ids", "fixture_ids"):
            reason = pci._LANE_PROJECTION_EXEMPT[name]
            self.assertTrue(reason.strip(), name)
            self.assertIsNone(pci._LANE_PROJECTION[name], name)
        for name in ("oracle", "falsifiability", "independent"):
            reason = pci._VERIFIER_PROJECTION_EXEMPT[name]
            self.assertTrue(reason.strip(), name)
            self.assertIsNone(pci._VERIFIER_PROJECTION[name], name)

    def test_every_exemption_states_a_reason(self):
        tables = (
            (pci._LANE_PROJECTION, pci._LANE_PROJECTION_EXEMPT,
             "_LANE_PROJECTION"),
            (pci._REQUIREMENT_PROJECTION, pci._REQUIREMENT_PROJECTION_EXEMPT,
             "_REQUIREMENT_PROJECTION"),
            (pci._VERIFIER_PROJECTION, pci._VERIFIER_PROJECTION_EXEMPT,
             "_VERIFIER_PROJECTION"),
            (pci._CLAIM_PROJECTION, pci._CLAIM_PROJECTION_EXEMPT,
             "_CLAIM_PROJECTION"),
            (pci._SEAM_PROJECTION, pci._SEAM_PROJECTION_EXEMPT,
             "_SEAM_PROJECTION"),
            (pci._FIXTURE_PROJECTION, pci._FIXTURE_PROJECTION_EXEMPT,
             "_FIXTURE_PROJECTION"),
        )
        for projection, exempt, table in tables:
            pci._assert_table_reasons(projection, exempt, table)
            for name, dest in projection.items():
                if dest is None:
                    self.assertTrue(exempt[name].strip(), table + "." + name)

    def test_the_guard_covers_tests_nodes(self):
        """PR #124 added TestsNode; it cannot hide from this guard."""
        self.assertIn(pm.TestsNode, pci._DESTINATION_NODE_TYPES)
        self.assertIn(pm.AgentNode, pci._DESTINATION_NODE_TYPES)
        self.assertIn(pm.CodeNode, pci._DESTINATION_NODE_TYPES)
        self.assertEqual(pci._EMITTED_NODE_KIND, "agent")
        self.assertIn("tests", pci._UNEMITTED_NODE_KINDS)
        self.assertTrue(pci._UNEMITTED_NODE_KINDS["tests"].strip())
        self.node()  # exercises the AgentNode/TestsNode field check

    def test_an_emitted_tests_kind_raises_until_the_ir_can_split(self):
        ir = _ir()
        draft = pci.project_draft(ir, self.repo)
        wrong = dict(draft["nodes"][0])
        wrong["kind"] = "tests"
        with self.assertRaises(pci.IngressProjectionIncomplete) as caught:
            pci._assert_ingress_projection_is_total(
                ir, ir["lanes"][0], ir["verifiers"][0], wrong)
        self.assertIn("tests", str(caught.exception))
        self.assertIn("_UNEMITTED_NODE_KINDS", str(caught.exception))


class RecordedPlanCatalogTest(unittest.TestCase):
    """The catalogs cover the IR issue #96 was measured against."""

    @unittest.skipUnless(P5.is_file(), "the recorded p5 IR is not checked out")
    def test_the_recorded_p5_ir_has_no_unaccounted_field(self):
        ir = json.loads(P5.read_text(encoding="utf-8"))
        catalogs = (
            ("lanes", pci._LANE_PROJECTION),
            ("requirements", pci._REQUIREMENT_PROJECTION),
            ("verifiers", pci._VERIFIER_PROJECTION),
            ("claims", pci._CLAIM_PROJECTION),
            ("seams", pci._SEAM_PROJECTION),
            ("fixtures", pci._FIXTURE_PROJECTION),
        )
        for collection, projection in catalogs:
            items = ir.get(collection)
            self.assertIsInstance(items, list, collection)
            for index, item in enumerate(items):
                extra = sorted(set(item) - set(projection))
                self.assertEqual(
                    extra, [],
                    "{0}[{1}] declares {2} with no catalog entry".format(
                        collection, index, extra))


if __name__ == "__main__":
    unittest.main()
