"""A lane whose contract no attempt can satisfy is refused before the run.

Recorded failure, run-0120c32064d144c2aa55c344087e0b0a. Plan
`cmo-consolidation-l` declared `lane-p1-freeze-and-run-log`, whose requirement
was behaviour over the legacy writers — freeze them at a high-water mark, prove
no code path updates a historical record in place. The lane's declared outputs
were one new module and its test. The legacy writers appeared in the declared
outputs of none of the plan's fourteen lanes, so the builder could not write
the file the behaviour needed, §8.3's permission delta would have rejected it
had it tried, and every attempt produced an out-of-contract workaround the
reviewer correctly rejected. The node exhausted its retry budget on a task no
correct attempt could pass.

The check that would have caught it is structural and not lexical, which is
§1.2 rather than a preference: an admission decision is a lifecycle
transition, and no lifecycle transition may be caused by free text. So the
last test here is the one that matters most — a plan whose prose is full of
paths, and whose declared relations are complete, is admitted.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adw_modules import plan_contract_ingress as pci   # noqa: E402
from adw_modules import plan_validate as pv            # noqa: E402


def _ir(**overrides) -> dict:
    """Two lanes, the second depending on the first, and every path declared.

    The control for every refusal below: it is admitted, so a refusal that
    fires here would mean the fixture is wrong rather than the plan.
    """
    data = {
        "schema_version": "plan-contract.v1",
        "plan_id": "phase-1",
        "title": "Phase 1 freeze",
        "plan_kind": "brownfield",
        "source_artifacts": [
            {"source_id": "src-readme", "path": "README.md",
             "sha256": "a" * 64, "required": True},
            {"source_id": "src-writer", "path": "src/legacy_writer.py",
             "sha256": "b" * 64, "required": True},
        ],
        "requirements": [
            {
                "requirement_id": "req-tables",
                "text": "Add the consolidation tables.",
                "surface": [{"path": "src/tables.py", "mutation": "written"}],
            },
            {
                "requirement_id": "req-freeze",
                "text": "Log every run against the consolidation tables.",
                "surface": [
                    {"path": "src/run_log.py", "mutation": "written"},
                    {"path": "src/tables.py", "mutation": "inherited"},
                    {"path": "README.md", "mutation": "unmodified"},
                ],
            },
        ],
        "lanes": [
            {"lane_id": "lane-tables", "title": "Tables",
             "execution_context": ".", "requirement_ids": ["req-tables"],
             "depends_on": [], "verifier_ids": ["verify-tables"]},
            {"lane_id": "lane-freeze", "title": "Freeze writers",
             "execution_context": ".", "requirement_ids": ["req-freeze"],
             "depends_on": ["lane-tables"], "verifier_ids": ["verify-freeze"]},
        ],
        "verifiers": [
            {"verifier_id": "verify-tables", "lane_ids": ["lane-tables"],
             "source_ids": ["src-readme"], "min_executed": 1,
             "command": "python3 -m pytest tests/test_tables.py"},
            {"verifier_id": "verify-freeze", "lane_ids": ["lane-freeze"],
             "source_ids": ["src-readme"], "min_executed": 1,
             "command": "python3 -m pytest tests/test_run_log.py"},
        ],
        "extensions": {"maestro": {
            "repo": "example",
            "outputs": {
                "lane-tables": ["src/tables.py"],
                "lane-freeze": ["src/run_log.py"],
            },
            "integration_branch": "main",
            "integration_gate": {"runner": "pytest", "argv": ["tests"],
                                 "cwd": ".", "min_cases": 1},
        }},
    }
    data.update(overrides)
    return data


def _requirement(ir: dict, requirement_id: str) -> dict:
    for requirement in ir["requirements"]:
        if requirement["requirement_id"] == requirement_id:
            return requirement
    raise AssertionError(requirement_id)


class SurfaceAdmissionTest(unittest.TestCase):
    def blockers(self, ir: dict):
        return pv.validate_contract_surface(ir)

    def assert_admitted(self, ir: dict) -> None:
        found = self.blockers(ir)
        self.assertEqual(
            found, (),
            "expected admission, got: "
            + " | ".join(item.message for item in found))

    def assert_refused(self, ir: dict, obligation, *fragments):
        found = self.blockers(ir)
        self.assertTrue(found, "expected a refusal, the plan was admitted")
        self.assertIn(obligation, {item.obligation for item in found})
        joined = " | ".join(item.message for item in found)
        for fragment in fragments:
            self.assertIn(fragment, joined)
        return found

    # ── the control ────────────────────────────────────────────────────────

    def test_a_fully_declared_plan_is_admitted(self):
        self.assert_admitted(_ir())

    # ── the recorded failure ───────────────────────────────────────────────

    def test_a_write_no_lane_owns_is_refused_naming_the_lane(self):
        """The freeze lane's gap, reproduced: the requirement's behaviour
        lives in a legacy writer, and no lane in the plan declares it."""
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"].append(
            {"path": "src/legacy_writer.py", "mutation": "written"})
        found = self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_REACHABLE,
            "lane-freeze", "src/legacy_writer.py", "req-freeze")
        self.assertEqual(
            [item.pointer for item in found],
            ["/requirements/1/surface/3/path"])

    def test_a_write_another_lane_owns_is_refused_and_names_the_owner(self):
        """A lane cannot write a sibling's output either: §8.3's permission
        delta rejects it and §6.4 gives the path exactly one owner."""
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"][1]["mutation"] = "written"
        self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_REACHABLE,
            "lane-freeze", "src/tables.py", "lane-tables")

    def test_the_dependency_closure_satisfies_an_inherited_path(self):
        """The containment question is asked of the whole plan: a path the
        lane reads is reachable when a lane it depends on produces it."""
        self.assert_admitted(_ir())
        ir = _ir()
        ir["lanes"][1]["depends_on"] = []
        self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_REACHABLE,
            "lane-freeze", "src/tables.py", "depends_on closure")

    def test_the_closure_is_transitive_and_survives_a_cycle(self):
        ir = _ir()
        ir["lanes"].append(
            {"lane_id": "lane-later", "title": "Later",
             "execution_context": ".", "requirement_ids": ["req-later"],
             "depends_on": ["lane-freeze"], "verifier_ids": ["verify-later"]})
        ir["requirements"].append(
            {"requirement_id": "req-later", "text": "Later work.",
             "surface": [{"path": "src/later.py", "mutation": "written"},
                         {"path": "src/tables.py", "mutation": "inherited"}]})
        ir["extensions"]["maestro"]["outputs"]["lane-later"] = ["src/later.py"]
        self.assert_admitted(ir)
        # A cycle is §6.4's obligation to report against the projected plan.
        # This check must terminate rather than pre-empt it.
        ir["lanes"][0]["depends_on"] = ["lane-later"]
        self.assert_admitted(ir)

    # ── the unmodified arm ─────────────────────────────────────────────────

    def test_an_unmodified_path_must_be_pinned(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"][2]["path"] = "docs/absent.md"
        self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_REACHABLE,
            "docs/absent.md", "not a declared source artifact")

    def test_a_path_a_lane_rewrites_is_not_unmodified(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"][2]["path"] = "src/tables.py"
        self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_REACHABLE,
            "src/tables.py", "lane-tables", "declares it as an output")

    # ── the field is required, not optional ────────────────────────────────

    def test_a_requirement_without_a_surface_is_refused(self):
        """§3.6 B8: a field added later is optional forever, so this one is
        enforced from its first version."""
        ir = _ir()
        del _requirement(ir, "req-freeze")["surface"]
        self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_DECLARED, "req-freeze")

    def test_a_plan_with_no_requirements_is_refused(self):
        ir = _ir()
        ir["requirements"] = []
        self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_DECLARED, "no requirements")

    def test_a_malformed_entry_is_reported_and_the_rest_still_answered(self):
        ir = _ir()
        surface = _requirement(ir, "req-freeze")["surface"]
        surface[0] = {"path": "src/run_log.py", "mutation": "rewritten"}
        surface.append({"path": "../outside.py", "mutation": "written"})
        surface.append({"path": "src/legacy_writer.py", "mutation": "written"})
        found = self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_DECLARED, "rewritten")
        obligations = {item.obligation for item in found}
        self.assertIn(pv.SurfaceObligation.SURFACE_REACHABLE, obligations)
        self.assertIn("../outside.py",
                      " | ".join(item.message for item in found))

    def test_a_requirement_no_lane_declares_is_refused(self):
        ir = _ir()
        ir["lanes"][1]["requirement_ids"] = []
        self.assert_refused(
            ir, pv.SurfaceObligation.SURFACE_REACHABLE,
            "req-freeze", "declared by no lane")

    # ── the anti-lexical regression test ───────────────────────────────────

    def test_prose_naming_an_unreachable_path_does_not_refuse(self):
        """The classification is structural. A plan whose declared relations
        are complete is admitted no matter what its prose says — including
        prose that names a path no lane can write, in every free-text field
        the IR carries. Deciding otherwise would make an admission decision
        turn on model-readable text, which §1.2 refuses outright.
        """
        ir = _ir()
        prose = ("Freeze src/legacy_writer.py and src/never_declared.py, "
                 "rewriting /etc/passwd and ../escape.py in place.")
        _requirement(ir, "req-freeze")["text"] = prose
        ir["verifiers"][1]["oracle"] = prose
        ir["title"] = prose
        ir["lanes"][1]["title"] = prose
        ir["seams"] = [{"seam_id": "seam-freeze", "producer": prose,
                        "consumer": prose, "contract": prose}]
        ir["fixtures"] = [{"fixture_id": "fx-writer", "path": prose,
                           "meaning": prose, "consumer_obligation": prose,
                           "prohibited_behavior": prose,
                           "affected_lane_ids": ["lane-freeze"]}]
        ir["claims"] = [{"claim_id": "claim-freeze", "subject": prose,
                         "predicate": "exercises", "object": prose}]
        self.assert_admitted(ir)


class SurfaceAdmissionAtIngressTest(unittest.TestCase):
    """The refusal reaches the boundary every route crosses.

    `plan author --from-plan-contract` and `plan ship` both reach a plan file
    only through `project_draft`, so siting the check there is what makes it
    unavoidable rather than one launch path's courtesy (§19 M6).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_declared_plan_projects(self):
        draft = pci.project_draft(_ir(), self.repo)
        self.assertEqual([node["node_id"] for node in draft["nodes"]],
                         ["lane-tables", "lane-freeze"])

    def test_an_unreachable_write_refuses_the_projection(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"].append(
            {"path": "src/legacy_writer.py", "mutation": "written"})
        with self.assertRaises(pci.IngressError) as caught:
            pci.project_draft(ir, self.repo)
        message = str(caught.exception)
        self.assertIn("UNREACHABLE_SURFACE", message)
        self.assertIn("lane-freeze", message)
        self.assertIn("req-freeze", message)
        self.assertIn("src/legacy_writer.py", message)

    def test_an_undeclared_surface_refuses_the_projection(self):
        ir = _ir()
        del _requirement(ir, "req-tables")["surface"]
        with self.assertRaises(pci.IngressError) as caught:
            pci.project_draft(ir, self.repo)
        self.assertIn("SURFACE_DECLARED", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
