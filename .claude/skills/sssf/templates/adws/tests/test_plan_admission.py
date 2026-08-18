"""A contract no correct attempt can satisfy is refused before the run.

Recorded failure, run-0120c32064d144c2aa55c344087e0b0a. Plan
`cmo-consolidation-l` lost nodes to two distinct shapes, and eight of the
thirteen catalogued instances are the first, four the second.

**Shape (i).** `lane-p1-freeze-and-run-log` was required to freeze the legacy
writers and prove no code path updates a historical record in place. Its
declared outputs were one new module and its test; the legacy writers appeared
in the declared outputs of none of the plan's fourteen lanes. The builder could
not write the file, the permission delta would have rejected it, and every
attempt produced an out-of-contract workaround the reviewer correctly rejected.

**Shape (ii).** `lane-p1-canonical-object-key`'s requirement declares "a pure
derivation and policy module with injected clients" and "no production
migration, object mutation, or backfill execution is authorized", and in the
same paragraph prescribes "server-side copy it to the canonical key". Three
attempts, all correctly rejected.

The two shapes are over two domains and need two predicates: repository paths,
and external acts. Both classifications are structural — an admission decision
is a lifecycle transition, and §1.2 forbids one caused by free text — so the
anti-lexical tests at the end of each class are the ones that matter most.
"""
from __future__ import annotations

import copy
import json
import pathlib
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


PROSE = ("Freeze src/legacy_writer.py and src/never_declared.py, rewriting "
         "/etc/passwd and ../escape.py in place, then server-side copy the "
         "artifact to the canonical key and backfill anything missing from "
         "the provider.")


def _effects(**overrides) -> list:
    """A complete disposition for every prohibited effect.

    Exhaustive by construction, because the predicate requires exactly one
    entry per prohibited effect: an author who performs a prohibited act cannot
    stay silent about it, only declare it falsely.
    """
    declared = {"canonical_object_write": "none", "source_backfill": "none"}
    declared.update(overrides)
    return [{"effect": effect, "disposition": disposition}
            for effect, disposition in sorted(declared.items())]


def _ir(**overrides) -> dict:
    """Two lanes, the second depending on the first, everything declared.

    The control for every refusal below: it is admitted, so a refusal that
    fires here means the fixture is wrong rather than the plan.
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
                "effects": _effects(),
            },
            {
                "requirement_id": "req-freeze",
                "text": "Log every run against the consolidation tables.",
                "surface": [
                    {"path": "src/run_log.py", "mutation": "written"},
                    {"path": "src/tables.py", "mutation": "inherited"},
                    {"path": "README.md", "mutation": "unmodified"},
                ],
                "effects": _effects(),
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
            "prohibited_effects": [
                {"effect": "canonical_object_write",
                 "meaning": "No object may be written into the canonical "
                            "case-management-orders namespace by this plan."},
                {"effect": "source_backfill",
                 "meaning": "No artifact may be retrieved from a provider "
                            "source by this plan."},
            ],
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


def _disposition(ir: dict, requirement_id: str, effect: str,
                 disposition: str) -> None:
    for entry in _requirement(ir, requirement_id)["effects"]:
        if entry["effect"] == effect:
            entry["disposition"] = disposition
            return
    raise AssertionError(effect)


class AdmissionTestCase(unittest.TestCase):
    def blockers(self, ir: dict):
        return pv.validate_admission(ir)

    def assert_admitted(self, ir: dict) -> None:
        found = self.blockers(ir)
        self.assertEqual(
            found, (),
            "expected admission, got: "
            + " | ".join(item.message for item in found))

    def assert_refused(self, ir: dict, obligation, *fragments):
        found = self.blockers(ir)
        self.assertTrue(found, "expected a refusal, the plan was admitted")
        self.assertIn(obligation, {item.obligation for item in found},
                      " | ".join(str(item.obligation) + " " + item.message
                                 for item in found))
        joined = " | ".join(item.message for item in found)
        for fragment in fragments:
            self.assertIn(fragment, joined)
        return found


class TheObligationSetTest(AdmissionTestCase):
    def test_the_obligations_are_a_checkable_count(self):
        """The predecessor of this tuple had no reader anywhere in the tree,
        which is §3.6 B15 arriving on its own commit. This is the reader."""
        self.assertEqual(len(pv.ADMISSION_OBLIGATIONS), 6)
        self.assertEqual(set(pv.ADMISSION_OBLIGATIONS),
                         set(pv.AdmissionObligation))

    def test_the_control_is_admitted(self):
        self.assert_admitted(_ir())


class SurfaceReachabilityTest(AdmissionTestCase):
    """Shape (i): a lane that cannot write what its requirement needs."""

    def test_a_write_no_lane_owns_is_refused_naming_the_lane(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"].append(
            {"path": "src/legacy_writer.py", "mutation": "written"})
        found = self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "lane-freeze", "src/legacy_writer.py", "req-freeze")
        self.assertIn("/requirements/1/surface/3/path",
                      [item.pointer for item in found])

    def test_a_write_another_lane_owns_is_refused_and_names_the_owner(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"][1]["mutation"] = "written"
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "lane-freeze", "src/tables.py", "lane-tables")

    def test_the_dependency_closure_satisfies_an_inherited_path(self):
        ir = _ir()
        ir["lanes"][1]["depends_on"] = []
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
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
                         {"path": "src/tables.py", "mutation": "inherited"}],
             "effects": _effects()})
        ir["extensions"]["maestro"]["outputs"]["lane-later"] = ["src/later.py"]
        self.assert_admitted(ir)
        # A cycle is §6.4's obligation to report against the projected plan.
        # This check must terminate rather than pre-empt it.
        ir["lanes"][0]["depends_on"] = ["lane-later"]
        self.assert_admitted(ir)

    # ── the unmodified arm ─────────────────────────────────────────────────

    def test_an_unmodified_path_must_be_a_declared_source_artifact(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"][2]["path"] = "docs/absent.md"
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "docs/absent.md", "not a declared source artifact")

    def test_an_unmodified_pin_without_a_digest_pins_nothing(self):
        """The blocker's own words are that something must pin the bytes the
        assertion is about. An entry with no sha256 defeats exactly that, so
        admitting one would make the check's stated purpose false."""
        ir = _ir()
        del ir["source_artifacts"][0]["sha256"]
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "README.md", "carrying a sha256")

    def test_a_path_a_lane_rewrites_is_not_unmodified(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"][2]["path"] = "src/tables.py"
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "src/tables.py", "lane-tables", "declares it as an output")

    def test_one_path_declared_written_and_unmodified_is_refused(self):
        """Caught today through the unmodified arm rather than by design.
        Accidental coverage needs a test or it evaporates on the next edit."""
        ir = _ir()
        _requirement(ir, "req-tables")["surface"].append(
            {"path": "src/tables.py", "mutation": "unmodified"})
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "src/tables.py", "unmodified")

    # ── the converse arm ───────────────────────────────────────────────────

    def test_a_declared_output_no_requirement_claims_is_refused(self):
        """Forward containment alone is a consistency check, not a
        satisfiability one: an author who writes each surface as exactly the
        outputs already chosen passes it trivially, and the freeze lane would
        have burned identically. Under-declaration now needs a visibly unowned
        output."""
        ir = _ir()
        ir["extensions"]["maestro"]["outputs"]["lane-freeze"].append(
            "src/unclaimed.py")
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "lane-freeze", "src/unclaimed.py", "no requirement it binds")

    def test_a_lane_with_outputs_and_no_requirement_is_refused(self):
        ir = _ir()
        ir["lanes"][1]["requirement_ids"] = []
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "src/run_log.py", "none")

    # ── the field is required, not optional ────────────────────────────────

    def test_a_requirement_without_a_surface_is_refused(self):
        ir = _ir()
        del _requirement(ir, "req-freeze")["surface"]
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_DECLARED, "req-freeze")

    def test_a_plan_with_no_requirements_is_refused(self):
        ir = _ir()
        ir["requirements"] = []
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_DECLARED, "no requirements")

    def test_a_malformed_entry_is_reported_and_the_rest_still_answered(self):
        ir = _ir()
        surface = _requirement(ir, "req-freeze")["surface"]
        surface[0] = {"path": "src/run_log.py", "mutation": "rewritten"}
        surface.append({"path": "../outside.py", "mutation": "written"})
        surface.append({"path": "src/legacy_writer.py", "mutation": "written"})
        found = self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_DECLARED, "rewritten")
        obligations = {item.obligation for item in found}
        self.assertIn(pv.AdmissionObligation.SURFACE_REACHABLE, obligations)
        self.assertIn("../outside.py",
                      " | ".join(item.message for item in found))

    def test_a_requirement_no_lane_declares_is_refused(self):
        ir = _ir()
        ir["lanes"][1]["requirement_ids"] = ["req-orphan"]
        ir["requirements"].append(
            {"requirement_id": "req-orphan-2", "text": "Nobody binds this.",
             "surface": [{"path": "src/run_log.py", "mutation": "written"}],
             "effects": _effects()})
        self.assert_refused(
            ir, pv.AdmissionObligation.SURFACE_REACHABLE,
            "req-orphan-2", "declared by no lane")

    def test_prose_naming_an_unreachable_path_does_not_refuse(self):
        """The classification is structural. A plan whose declared relations
        are complete is admitted no matter what its prose says — including
        prose that names a path no lane can write, in every free-text field
        the IR carries."""
        ir = _ir()
        _requirement(ir, "req-freeze")["text"] = PROSE
        ir["verifiers"][1]["oracle"] = PROSE
        ir["title"] = PROSE
        ir["lanes"][1]["title"] = PROSE
        ir["seams"] = [{"seam_id": "seam-freeze", "producer": PROSE,
                        "consumer": PROSE, "contract": PROSE}]
        ir["fixtures"] = [{"fixture_id": "fx-writer", "path": PROSE,
                           "meaning": PROSE, "consumer_obligation": PROSE,
                           "prohibited_behavior": PROSE,
                           "affected_lane_ids": ["lane-freeze"]}]
        ir["claims"] = [{"claim_id": "claim-freeze", "subject": PROSE,
                         "predicate": "exercises", "object": PROSE}]
        self.assert_admitted(ir)


class EffectAuthorizationTest(AdmissionTestCase):
    """Shape (ii): a requirement that prescribes an act its plan forbids.

    None of the four real instances names a repository path on either side,
    so `validate_contract_surface` catches zero of them. The contentions are
    an object write into the canonical namespace, a source backfill, and a
    projection write — a wrong-domain answer, not a gap in cleverness.
    """

    def test_a_performed_prohibited_effect_is_refused(self):
        ir = _ir()
        _disposition(ir, "req-freeze", "canonical_object_write", "performed")
        found = self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_AUTHORIZED,
            "req-freeze", "canonical_object_write", "lane-freeze",
            # The prohibition's own transcribed meaning, so an author is told
            # which act they declared rather than only which enum member.
            "canonical case-management-orders namespace")
        self.assertIn("/requirements/1/effects/0/disposition",
                      [item.pointer for item in found])

    def test_a_performed_effect_the_plan_does_not_prohibit_is_admitted(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["effects"].append(
            {"effect": "migration_execution", "disposition": "performed"})
        self.assert_admitted(ir)

    def test_fake_only_is_admitted_under_a_prohibition(self):
        ir = _ir()
        _disposition(ir, "req-freeze", "canonical_object_write", "fake_only")
        self.assert_admitted(ir)

    def test_planned_is_admitted_when_something_else_discharges_it(self):
        ir = _ir()
        _disposition(ir, "req-freeze", "canonical_object_write", "planned")
        _disposition(ir, "req-tables", "canonical_object_write", "fake_only")
        self.assert_admitted(ir)

    def test_an_effect_planned_everywhere_and_executed_nowhere_is_refused(self):
        """A planned effect emits a record describing an act something else
        carries out. Nothing carrying it out makes the plan a decision it
        describes and never makes."""
        ir = _ir()
        _disposition(ir, "req-freeze", "source_backfill", "planned")
        _disposition(ir, "req-tables", "source_backfill", "planned")
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DISCHARGED,
            "source_backfill", "executed by nothing")

    def test_discharge_does_not_fire_on_an_effect_nobody_plans(self):
        ir = _ir()
        _disposition(ir, "req-freeze", "source_backfill", "fake_only")
        self.assert_admitted(ir)

    def test_none_everywhere_is_admitted(self):
        """A prohibited effect no lane touches is legal, and `none` is how an
        author says so. Without it an author forced to declare all five
        reached for `planned`, which is how that member stopped
        discriminating."""
        self.assert_admitted(_ir())

    # ── declaration errors: omission and value are distinguished ───────────

    def test_a_requirement_without_effects_is_refused_as_an_omission(self):
        ir = _ir()
        del _requirement(ir, "req-freeze")["effects"]
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED,
            "req-freeze", "declares no effects")

    def test_an_incomplete_declaration_names_the_missing_effect(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["effects"] = [
            entry for entry in _requirement(ir, "req-freeze")["effects"]
            if entry["effect"] != "source_backfill"]
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED,
            "source_backfill", "omits a required entry",
            "This is an omission, not a value error")

    def test_an_unknown_effect_name_is_refused_as_a_value_error(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["effects"].append(
            {"effect": "s3_object_write", "disposition": "none"})
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED,
            "s3_object_write", "value error, not an omission")

    def test_an_unknown_disposition_is_refused_as_a_value_error(self):
        ir = _ir()
        _disposition(ir, "req-freeze", "source_backfill", "prohibited")
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED,
            "'prohibited'", "value error, not an omission")

    def test_one_effect_declared_twice_is_refused(self):
        ir = _ir()
        _requirement(ir, "req-freeze")["effects"].append(
            {"effect": "source_backfill", "disposition": "fake_only"})
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED,
            "source_backfill", "twice")

    # ── the plan-level prohibition ─────────────────────────────────────────

    def test_a_plan_without_prohibited_effects_is_refused(self):
        ir = _ir()
        del ir["extensions"]["maestro"]["prohibited_effects"]
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED,
            "declares no prohibited_effects")

    def test_a_prohibition_without_a_meaning_is_refused(self):
        """`meaning` has three readers: this check, the refusal message that
        quotes it, and the node contract the reviewer is handed. Without the
        last, the plan reviewer and the node reviewer would resolve an effect
        name against two different documents."""
        ir = _ir()
        ir["extensions"]["maestro"]["prohibited_effects"][0]["meaning"] = "  "
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED,
            "states no meaning")

    def test_an_effect_prohibited_twice_is_refused(self):
        ir = _ir()
        ir["extensions"]["maestro"]["prohibited_effects"].append(
            {"effect": "source_backfill", "meaning": "again"})
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_DECLARED, "prohibited twice")

    # ── a lane's effects are the union of its requirements' ────────────────

    def test_two_requirements_on_one_lane_may_not_disagree(self):
        ir = _ir()
        ir["lanes"][1]["requirement_ids"] = ["req-freeze", "req-second"]
        ir["requirements"].append(
            {"requirement_id": "req-second", "text": "The same lane, again.",
             "surface": [{"path": "src/run_log.py", "mutation": "written"}],
             "effects": _effects(source_backfill="fake_only")})
        _disposition(ir, "req-freeze", "source_backfill", "planned")
        self.assert_refused(
            ir, pv.AdmissionObligation.EFFECT_CONSISTENT,
            "lane-freeze", "req-freeze", "req-second", "source_backfill",
            "planned", "fake_only")

    def test_two_requirements_on_one_lane_that_agree_are_admitted(self):
        ir = _ir()
        ir["lanes"][1]["requirement_ids"] = ["req-freeze", "req-second"]
        ir["requirements"].append(
            {"requirement_id": "req-second", "text": "The same lane, again.",
             "surface": [{"path": "src/run_log.py", "mutation": "written"}],
             "effects": _effects()})
        self.assert_admitted(ir)

    def test_prose_prescribing_a_prohibited_act_does_not_refuse(self):
        """The anti-lexical control for this domain. The requirement's text
        names a server-side copy and a backfill in the document's own words;
        the declared dispositions say `none`; the plan is admitted. Deciding
        otherwise would make an admission decision turn on model-readable
        text, which §1.2 refuses outright."""
        ir = _ir()
        for requirement in ir["requirements"]:
            requirement["text"] = PROSE
        ir["verifiers"][0]["oracle"] = PROSE
        ir["extensions"]["maestro"]["prohibited_effects"][0]["meaning"] = PROSE
        self.assert_admitted(ir)


class AdmissionAtIngressTest(unittest.TestCase):
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
        self.assertIn("ADMISSION_REFUSED", message)
        self.assertIn("lane-freeze", message)
        self.assertIn("src/legacy_writer.py", message)

    def test_a_prohibited_effect_refuses_the_projection(self):
        ir = _ir()
        _disposition(ir, "req-tables", "source_backfill", "performed")
        with self.assertRaises(pci.IngressError) as caught:
            pci.project_draft(ir, self.repo)
        self.assertIn("ADMISSION_REFUSED", str(caught.exception))
        self.assertIn("source_backfill", str(caught.exception))

    def test_both_blocker_sets_arrive_in_one_refusal(self):
        """An author sent back twice for two defects in one document is the
        fail-fast validator §11.1 rejects."""
        ir = _ir()
        _requirement(ir, "req-freeze")["surface"].append(
            {"path": "src/legacy_writer.py", "mutation": "written"})
        _disposition(ir, "req-tables", "source_backfill", "performed")
        with self.assertRaises(pci.IngressError) as caught:
            pci.project_draft(ir, self.repo)
        obligations = {item.obligation for item in caught.exception.blockers}
        self.assertIn(pv.AdmissionObligation.SURFACE_REACHABLE, obligations)
        self.assertIn(pv.AdmissionObligation.EFFECT_AUTHORIZED, obligations)

    def test_the_refusal_carries_its_blockers_typed(self):
        """Thirteen blockers joined into one string is a wall no caller can
        take apart, and `validate_plan` already returns its blockers typed."""
        ir = _ir()
        del _requirement(ir, "req-freeze")["surface"]
        with self.assertRaises(pci.IngressError) as caught:
            pci.project_draft(ir, self.repo)
        blockers = caught.exception.blockers
        self.assertTrue(blockers)
        for blocker in blockers:
            self.assertIsInstance(blocker, pv.AdmissionBlocker)
            self.assertIsInstance(blocker.obligation, pv.AdmissionObligation)


class TheAuthoringSchemaAdmitsWhatMaestroRequiresTest(unittest.TestCase):
    """Maestro cannot require a field the authoring validator forbids.

    This is a real deadlock, not a hypothetical: with `surface` required here
    and rejected by `planctl`, `maestro plan gate` fails *with* the field and
    `maestro plan ship` fails *without* it, and no plan can be authored at all.
    The template suite could not see it, because the plan-contract verb tests
    drive a stand-in planctl and never the real validator. A second stand-in
    would not have seen it either — so this reads the authoring schema itself.

    It skips only when the-library is not checked out beside this repository,
    on the same rule `test_template_parity` uses: a peer that is present but
    missing the file it must carry is the failure this exists to catch.
    """

    SCHEMA = pathlib.PurePosixPath(
        "skills/plan-contract/schemas/plan-ir-v1.schema.json")

    def schema(self):
        siblings = ADWS.parents[4].parent
        path = siblings / "the-library" / self.SCHEMA
        if not (siblings / "the-library").is_dir():
            self.skipTest("the-library is not checked out at " + str(siblings))
        self.assertTrue(
            path.is_file(),
            "the-library is present but carries no authoring schema at "
            + str(path))
        return json.loads(path.read_text(encoding="utf-8"))

    def defs(self):
        return self.schema().get("$defs", {})

    def enum_of(self, node):
        """The enum a property carries, following one `$ref` into `$defs`.

        The two sides are free to factor their vocabulary differently — the
        authoring schema keeps `effectName` as a shared definition — so this
        compares the values, which is the thing that has to agree, rather than
        where each side chose to write them down.
        """
        defs = self.defs()
        seen = 0
        while "$ref" in node and seen < 4:
            node = defs[node["$ref"].rsplit("/", 1)[-1]]
            seen += 1
        return tuple(node["enum"])

    def test_the_schema_requires_a_surface_on_every_requirement(self):
        requirement = self.defs()["requirement"]
        self.assertIn("surface", requirement["required"])
        self.assertIn("surface", requirement["properties"])

    def test_the_mutation_vocabularies_agree(self):
        entry = self.defs()["requirementSurfaceEntry"]
        self.assertEqual(self.enum_of(entry["properties"]["mutation"]),
                         pv.MUTATIONS)

    def test_the_schema_requires_effects_on_every_requirement(self):
        requirement = self.defs()["requirement"]
        self.assertIn("effects", requirement["required"])

    def test_the_effect_vocabularies_agree(self):
        entry = self.defs()["requirementEffect"]
        self.assertEqual(self.enum_of(entry["properties"]["effect"]),
                         pv.EFFECTS)
        self.assertEqual(self.enum_of(entry["properties"]["disposition"]),
                         pv.DISPOSITIONS)

    def test_the_schema_carries_prohibited_effects_with_a_meaning(self):
        entry = self.defs()["prohibitedEffect"]
        self.assertEqual(self.enum_of(entry["properties"]["effect"]),
                         pv.EFFECTS)
        self.assertIn("meaning", entry["required"])
        self.assertEqual(entry["properties"]["meaning"]["minLength"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
