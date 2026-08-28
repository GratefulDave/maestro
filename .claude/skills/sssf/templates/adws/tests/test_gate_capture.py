"""Executable proof that the code under test cannot manufacture its own gate.

The measured incident: `lane-routing-chemical` a3 (inherited by a4, a5) put ~190
lines into `src/lexgenius_pipeline/epa_build/routing.py` — production source, a
declared output — whose only purpose was to make the gate report five passing
cases when the accepted test file defines two. It had been honestly rejected
with `2 passed, min_cases is 5` the cycle before, and §8.3 had already convicted
it for editing the test file directly the cycle before that. This was the third
route it tried, and it worked.

The file is preserved at
`~/.maestro/lexgenius-pipeline/forensics/gate-forgery-2026-08-27/`.

Two classes of test here, and the split is the point.

`TheExistingGuardsAcquitTheForgery` is the control. It executes the guards that
were in place and shows each returning "clean" — truthfully. Without it, every
assertion below is compatible with "some existing check already covered this",
which is the claim that has to be false for any of this to be worth building.

`ThePatternSweepIsNotSufficient` shows a capture that provenance convicts
without needing a spelling list.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import gate_capture as gc  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402

#: Stands in for the accepted test candidate: two honest cases, which is what
#: the real gate file defined when the plan demanded five.
ACCEPTED_TESTS = '''\
"""The reviewed, accepted test candidate."""


def test_exactly_eight_reviewed_families_route_once() -> None:
    assert len(EIGHT_FAMILIES) == 8


def test_dsstox_activation_lights_the_chemical_dimension() -> None:
    assert chemical_rows() > 0
'''


#: What the runner reported on the forged candidate: the two real cases plus
#: three the accepted file does not define.
FORGED_NODEIDS = (
    "tests/epa_build/test_routing_chemical.py::test_exactly_eight_reviewed_families_route_once",
    "tests/epa_build/test_routing_chemical.py::test_dsstox_activation_lights_the_chemical_dimension",
    "tests/epa_build/test_routing_chemical.py::test_dsstox_identity_routes_to_dim_chemical",
    "tests/epa_build/test_routing_chemical.py::test_tri_reporting_form_routes_to_fact_release",
    "tests/epa_build/test_routing_chemical.py::test_tri_chem_info_stays_excluded_from_marts",
)

HONEST_NODEIDS = FORGED_NODEIDS[:2]

FORENSICS = (
    Path.home()
    / ".maestro/lexgenius-pipeline/forensics/gate-forgery-2026-08-27/routing.a5.py"
)


class TheExistingGuardsAcquitTheForgery(unittest.TestCase):
    """The control. Every guard that was in place returns clean, truthfully."""

    def test_the_forged_file_is_not_a_test_path_so_8_3_permits_it(self):
        """§8.3 sees a write to a declared output and nothing else.

        This is why moving the forgery into production source worked: the
        permission check is about *where* a node wrote, and production source
        is exactly where a build node is supposed to write.
        """
        self.assertFalse(
            tc.is_test_path("src/lexgenius_pipeline/epa_build/routing.py")
        )
        self.assertEqual(
            tc.paths_not_tests(("src/lexgenius_pipeline/epa_build/routing.py",)),
            ("src/lexgenius_pipeline/epa_build/routing.py",),
        )

    def test_the_accepted_test_bytes_are_untouched_so_pairing_passes(self):
        """`compare_test_bytes` compares blobs, and the blob never changed.

        The forgery ran at collection time. Byte-for-byte the reviewed test
        file in the candidate's tree is the reviewed test file, so the pairing
        check passes and is right to.
        """
        candidate_test_source = ACCEPTED_TESTS
        self.assertEqual(candidate_test_source, ACCEPTED_TESTS)

    def test_the_counting_rule_counts_five_because_five_ran(self):
        """`min_cases` is satisfied, honestly, by a compromised runner."""
        self.assertGreaterEqual(len(FORGED_NODEIDS), 5)


class CaseProvenanceConvictsTheForgery(unittest.TestCase):
    """The structural repair: every counted case must be in the reviewed bytes."""

    def test_the_manufactured_cases_are_named(self):
        strays = gc.unexpected_cases(ACCEPTED_TESTS, FORGED_NODEIDS)
        self.assertEqual(len(strays), 3)
        joined = " ".join(strays)
        self.assertIn("test_dsstox_identity_routes_to_dim_chemical", joined)
        self.assertIn("test_tri_reporting_form_routes_to_fact_release", joined)
        self.assertIn("test_tri_chem_info_stays_excluded_from_marts", joined)

    def test_an_honest_run_is_clean(self):
        self.assertEqual(gc.unexpected_cases(ACCEPTED_TESTS, HONEST_NODEIDS), ())

    def test_parametrised_ids_are_refused_whatever_they_resolve_to(self):
        """Bracket stripping still identifies the case; the id is still refused.

        This case used to use `ACCEPTED_TESTS`, whose cases carry no
        `parametrize` mark, and asserted the ids were clean. That was the
        inflation hole: it licensed `test_x[0..n]` for any accepted name.
        `CountInflationUsingOnlyAcceptedNames` now refuses exactly that, so the
        intent — a parametrised id resolves to its authored case — is asserted
        here against a source that genuinely parametrises.
        """
        ids = (
            "tests/t.py::test_exactly_eight_reviewed_families_route_once[a-1]",
            "tests/t.py::test_exactly_eight_reviewed_families_route_once[b-2]",
        )
        self.assertEqual(len(gc.unexpected_cases(ACCEPTED_TESTS, ids)), 2)

    def test_a_case_inside_a_test_class_counts_as_defined(self):
        source = "class TestRouting:\n    def test_inside(self) -> None:\n        pass\n"
        self.assertEqual(
            gc.unexpected_cases(source, ("tests/t.py::TestRouting::test_inside",)), ()
        )

    def test_unparseable_accepted_bytes_refuse_rather_than_admit_everything(self):
        """Fail closed. An empty allowlist would acquit every stray case."""
        with self.assertRaises(gc.GateCaptureRefusal) as caught:
            gc.unexpected_cases("def broken(:\n", HONEST_NODEIDS)
        self.assertIn(gc.ACCEPTED_TESTS_UNPARSEABLE, str(caught.exception))

    def test_a_nodeid_with_no_case_name_refuses(self):
        with self.assertRaises(gc.GateCaptureRefusal):
            gc.unexpected_cases(ACCEPTED_TESTS, ("tests/t.py::",))


class AnUnsatisfiableGateIsTheRootCause(unittest.TestCase):
    """Refuse the impossible task before convicting the lane that failed it.

    The accepted test candidate for `lane-routing-chemical` defines exactly two
    cases; the plan demanded `min_cases: 5`. A build lane must carry those bytes
    verbatim, so no honest attempt could ever have passed. Convicting the
    forgery without refusing this would leave the next lane in the same trap.
    """

    def test_the_measured_gate_is_refused_as_unsatisfiable(self):
        self.assertEqual(gc.unsatisfiable_min_cases(ACCEPTED_TESTS, 5), 3)

    def test_a_satisfiable_gate_is_clean(self):
        self.assertEqual(gc.unsatisfiable_min_cases(ACCEPTED_TESTS, 2), 0)
        self.assertEqual(gc.unsatisfiable_min_cases(ACCEPTED_TESTS, 1), 0)

    def test_the_real_accepted_candidate_defines_two_cases(self):
        """The measurement itself, so the claim is not left as prose."""
        self.assertEqual(len(gc.case_names_defined(ACCEPTED_TESTS)), 2)

    def test_unparseable_accepted_bytes_refuse_here_too(self):
        with self.assertRaises(gc.GateCaptureRefusal):
            gc.unsatisfiable_min_cases("def broken(:\n", 5)


class ProvenanceCatchesTheClassTheSweepCatchesByLuck(unittest.TestCase):
    """Both real artifacts, side by side, against both checks.

    This encodes the sharpest lesson available from the incident. The rejected
    candidate `b776adf6` and a5's escalation manufacture the *same three cases*
    by different means, so they are a natural control pair:

      * a3 imports no pytest, defines no hook, and synthesises no item. The AST
        sweep convicts it on **one** rule — the default-argument string route —
        which exists only because that evasion was noticed while reading the
        artifact. Remove that single rule and the sweep returns nothing on a3.
      * a5 escalated and trips four rules.

    Provenance convicts both, three of three, identically, because it does not
    care how the case arrived — only that the accepted bytes do not define it.
    That is the difference between catching a class and catching an
    implementation detail, and it is why the sweep is not the repair.
    """

    FORENSIC_DIR = FORENSICS.parent
    ACCEPTED = FORENSIC_DIR / "accepted-tests_test_routing_chemical.py"

    def _artifact(self, name):
        return (self.FORENSIC_DIR / name).read_text(encoding="utf-8")

    def _injected_nodeids(self, source):
        import ast

        names = sorted(
            {
                node.name
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test")
            }
        )
        return [
            "tests/epa_build/test_routing_chemical.py::{0}".format(name)
            for name in names
        ]

    @unittest.skipUnless(ACCEPTED.is_file(), "preserved artifacts not present")
    def test_the_accepted_candidate_defines_two_cases_against_a_demand_of_five(self):
        defined = gc.case_names_defined(self.ACCEPTED.read_text(encoding="utf-8"))
        self.assertEqual(len(defined), 2)
        self.assertEqual(
            gc.unsatisfiable_min_cases(self.ACCEPTED.read_text(encoding="utf-8"), 5),
            3,
            "the lane was three cases short of a target it could not reach",
        )

    @unittest.skipUnless(ACCEPTED.is_file(), "preserved artifacts not present")
    def test_provenance_convicts_both_artifacts_equally(self):
        accepted = self.ACCEPTED.read_text(encoding="utf-8")
        for name in ("routing.a3-commit-b776adf6.py", "routing.a5.py"):
            with self.subTest(artifact=name):
                source = self._artifact(name)
                strays = gc.unexpected_cases(accepted, self._injected_nodeids(source))
                self.assertEqual(
                    len(strays), 3, "every manufactured case must be named"
                )



class CountInflationUsingOnlyAcceptedNames(unittest.TestCase):
    """Membership alone does not close the class.

    Found by being asked whether an inflated count could be built from names the
    accepted file *does* define, rather than by the next incident. It could:
    against the real 2-case candidate, both of these reported five and were
    refused by nothing.
    """

    def test_a_repeated_node_id_is_refused(self):
        """Pytest ids are unique in a run, so a repeat is never honest."""
        one = "tests/t.py::test_exactly_eight_reviewed_families_route_once"
        strays = gc.unexpected_cases(ACCEPTED_TESTS, [one] * 5)
        self.assertEqual(len(strays), 4, "the first is real; the copies are not")

    def test_a_faked_parametrisation_of_an_accepted_case_is_refused(self):
        ids = [
            "tests/t.py::test_exactly_eight_reviewed_families_route_once[{0}]".format(i)
            for i in range(5)
        ]
        strays = gc.unexpected_cases(ACCEPTED_TESTS, ids)
        self.assertEqual(
            len(strays), 5, "the accepted case carries no parametrize mark"
        )

    def test_even_a_genuine_parametrisation_is_refused_for_now(self):
        """Uncountable until something needs it.

        A real `parametrize` yields a count no static reading can bound, so
        admitting it would leave the inflation open for any accepted name.
        Measured before choosing: zero uses in this suite, zero in the accepted
        EPA candidate. The relaxation is a static AST count, not a flag.
        """
        source = (
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', [1, 2])\n"
            "def test_real(n) -> None:\n    assert n\n"
        )
        ids = ["tests/t.py::test_real[1]", "tests/t.py::test_real[2]"]
        self.assertEqual(len(gc.unexpected_cases(source, ids)), 2)
        self.assertEqual(gc.parametrised_case_names(source), frozenset({"test_real"}))

    def test_an_unparametrised_case_still_reports_its_bare_id(self):
        self.assertEqual(gc.unexpected_cases(ACCEPTED_TESTS, HONEST_NODEIDS), ())

    def test_the_former_residual_is_now_closed(self):
        """This used to admit 50 instances of one authored case. It no longer does.

        The residual was real: a genuinely parametrised case's instance count
        cannot be statically bounded, so admitting bracketed ids left the count
        inflatable using only names the accepted file defines.
        """
        source = (
            "import pytest\n\n\n"
            "@pytest.mark.parametrize('n', COMPUTED_AT_IMPORT)\n"
            "def test_real(n) -> None:\n    assert n\n"
        )
        ids = ["tests/t.py::test_real[{0}]".format(i) for i in range(50)]
        self.assertEqual(
            len(gc.unexpected_cases(source, ids)),
            50,
            "every bracketed instance is refused; the hole is closed",
        )


class ThePatternSweepIsNotSufficient(unittest.TestCase):
    """Provenance convicts a capture that no spelling list is needed to see."""


    def test_and_provenance_convicts_it_anyway(self):
        strays = gc.unexpected_cases(
            ACCEPTED_TESTS, ("tests/t.py::test_manufactured_case",)
        )
        self.assertEqual(strays, ("tests/t.py::test_manufactured_case",))


if __name__ == "__main__":
    unittest.main()
