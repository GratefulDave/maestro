"""A case red at the parent for a reason the plan did not claim.

FDAdb run be064e58, `lane-wp3-adapter-build`: three code-review rounds and a
`NO_PROGRESS` park on `executed=6 passed=5 failed=1`, the same red case from
three different candidates. Case 6 asserted a non-regression property -- it
should have held at the parent -- and its assertion called a shipped module
the lane did not own, with a key that module does not recognise. "The suite is
red at the parent" was true the whole time and said nothing.

These cases hold the two halves apart: the plan states each case's expected
outcome at the parent (`plan_validate`), and the harness compares the
measurement against that statement (`scheduler`). The runner-output parsing is
exercised against text shapes taken from real pytest and vitest runs; the
end-to-end proof against the real binaries is in the PR that added this file.
"""

import unittest

from adw_modules import plan_validate as pv
from adw_modules import runner_resolution as rr
from adw_modules import scheduler as sch
from adw_modules import plan_contract_ingress as pci


def _tests_lane(declared, *, min_cases=2):
    gate = {"runner": "pytest", "argv": ["tests/a.py"], "cwd": ".",
            "min_cases": min_cases}
    if declared is not None:
        gate["declared_cases"] = declared
    return {"spec": {"gate": gate}}


def _codes(refusals):
    return [item.code for item in refusals]


class DeclaredCasesCompilerRefusal(unittest.TestCase):
    def _run(self, spec, kind="tests", bound_run=False):
        refusals = []
        pv._validate_declared_cases(
            "/lanes/0", spec["spec"], kind, refusals, bound_run=bound_run
        )
        return refusals

    def test_absent_declared_cases_is_admissible(self):
        """Shipped plans carry no declared_cases and must stay runnable."""
        self.assertEqual(self._run(_tests_lane(None)), [])

    def test_short_of_min_cases_refuses(self):
        refusals = self._run(
            _tests_lane([{"case": "a", "red_at_parent": True}], min_cases=2)
        )
        self.assertEqual(
            _codes(refusals), [pv.CASE_FALSIFICATION_UNDECLARED]
        )
        self.assertIn("gate.min_cases 2", refusals[0].message)

    def test_unstated_outcome_refuses(self):
        refusals = self._run(
            _tests_lane(
                [{"case": "a", "red_at_parent": True}, {"case": "b"}]
            )
        )
        self.assertIn(pv.CASE_FALSIFICATION_UNDECLARED, _codes(refusals))
        self.assertTrue(
            any("red_at_parent" in item.pointer for item in refusals)
        )

    def test_no_red_case_refuses(self):
        refusals = self._run(
            _tests_lane(
                [{"case": "a", "red_at_parent": False},
                 {"case": "b", "red_at_parent": False}]
            )
        )
        self.assertEqual(_codes(refusals), [pv.CASE_FALSIFICATION_UNDECLARED])
        self.assertIn("no declared case is red_at_parent", refusals[0].message)

    def test_duplicate_case_refuses(self):
        refusals = self._run(
            _tests_lane(
                [{"case": "a", "red_at_parent": True},
                 {"case": "a", "red_at_parent": False}]
            )
        )
        self.assertIn(pv.CASE_FALSIFICATION_UNDECLARED, _codes(refusals))

    def test_well_formed_declaration_is_admissible(self):
        self.assertEqual(
            self._run(
                _tests_lane(
                    [{"case": "a", "red_at_parent": True},
                     {"case": "b", "red_at_parent": False}]
                )
            ),
            [],
        )

    def test_build_lane_is_not_judged(self):
        self.assertEqual(self._run(_tests_lane([]), kind="build"), [])

    def test_bound_run_is_not_rejudged(self):
        """An authoring obligation is never re-judged mid-run."""
        self.assertEqual(self._run(_tests_lane([]), bound_run=True), [])


class DeclaredCasesReachTheSpec(unittest.TestCase):
    def _verifier(self, **extra):
        payload = {
            "verifier_id": "v1",
            "command": "pytest tests/a.py",
            "min_executed": 2,
        }
        payload.update(extra)
        return payload

    def test_tests_verifier_carries_declared_cases_onto_the_gate(self):
        gate = pci._gate(
            self._verifier(
                declared_cases=[{"case": " a ", "red_at_parent": True}]
            ),
            ".",
            "lane-x",
            "tests",
        )
        self.assertEqual(
            gate["declared_cases"], [{"case": "a", "red_at_parent": True}]
        )

    def test_absent_leaves_the_gate_unchanged(self):
        gate = pci._gate(self._verifier(), ".", "lane-x", "tests")
        self.assertNotIn("declared_cases", gate)

    def test_build_verifier_is_refused_not_dropped(self):
        with self.assertRaises(pci.IngressError) as caught:
            pci._gate(
                self._verifier(
                    declared_cases=[{"case": "a", "red_at_parent": True}]
                ),
                ".",
                "lane-x",
                "build",
            )
        self.assertIn("declared_cases", str(caught.exception))

    def test_unstated_outcome_is_refused_at_ingress(self):
        with self.assertRaises(pci.IngressError):
            pci._gate(
                self._verifier(declared_cases=[{"case": "a"}]),
                ".",
                "lane-x",
                "tests",
            )


class CaseOutcomeParsing(unittest.TestCase):
    """Shapes copied from real runs; see the PR for the live-binary proof."""

    def test_pytest_verbose(self):
        output = (
            "tests/test_adapter.py::test_classifies_gap FAILED         [ 50%]\n"
            "tests/test_adapter.py::test_holds PASSED                  [100%]\n"
            "tests/test_adapter.py::test_skipped SKIPPED (why)         [100%]\n"
            "=========================== short test summary ===========\n"
        )
        self.assertEqual(
            rr.case_outcomes("pytest", output),
            {
                "tests/test_adapter.py::test_classifies_gap": True,
                "tests/test_adapter.py::test_holds": False,
                "tests/test_adapter.py::test_skipped": False,
            },
        )

    def test_vitest_verbose_drops_the_file_rollup_and_the_duration(self):
        output = (
            " ✓ tests/adapter.test.ts (2 tests) 9ms\n"
            "   × tests/adapter.test.ts > adapter > classifies gap 3ms\n"
            "   ✓ tests/adapter.test.ts > adapter > holds 1ms\n"
            "  Tests  1 failed | 1 passed (2)\n"
        )
        self.assertEqual(
            rr.case_outcomes("vitest", output),
            {
                "tests/adapter.test.ts > adapter > classifies gap": True,
                "tests/adapter.test.ts > adapter > holds": False,
            },
        )

    def test_an_unreadable_run_is_not_an_empty_mapping(self):
        """Empty would read downstream as 'nothing was red'."""
        self.assertEqual(rr.case_outcomes("pytest", "1 failed in 0.01s"), {})


class RedAtParentDivergences(unittest.TestCase):
    OUTCOMES = {
        "tests/a.py::test_classifies_gap": True,
        "tests/a.py::test_does_not_treat_label_evidence_as_gap": True,
    }

    def test_declared_green_observed_red_is_the_be064e58_shape(self):
        problems = sch._red_at_parent_divergences(
            (
                ("test_classifies_gap", True),
                ("test_does_not_treat_label_evidence_as_gap", False),
            ),
            self.OUTCOMES,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("declared green at the parent, observed red", problems[0])

    def test_declared_red_observed_green(self):
        problems = sch._red_at_parent_divergences(
            (("test_holds", True),), {"tests/a.py::test_holds": False}
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("observed green", problems[0])

    def test_declared_case_the_parent_never_ran(self):
        problems = sch._red_at_parent_divergences(
            (("test_absent", True),), self.OUTCOMES
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("ran no case naming it", problems[0])

    def test_agreement_is_no_divergence(self):
        self.assertEqual(
            sch._red_at_parent_divergences(
                (
                    ("test_classifies_gap", True),
                    ("test_does_not_treat_label_evidence_as_gap", True),
                ),
                self.OUTCOMES,
            ),
            (),
        )

    def test_findings_are_revise_shaped_and_harness_owned(self):
        findings = sch._red_at_parent_findings(("'x': declared green",))
        self.assertEqual(
            findings[0]["violated_requirement"], "gate.declared_cases"
        )
        self.assertIn(
            "gate.declared_cases", sch.HARNESS_VIOLATED_REQUIREMENTS
        )


class LaneGateReadsDeclaredCases(unittest.TestCase):
    def _actor(self, gate):
        return type("A", (), {"lane_specs": {"lane-x": {"gate": gate}}})()

    BASE = {"runner": "pytest", "argv": ["tests/a.py"], "cwd": ".",
            "min_cases": 1}

    def test_absent_is_an_empty_tuple(self):
        gate = sch._lane_gate(self._actor(dict(self.BASE)), "lane-x")
        self.assertEqual(gate.declared_cases, ())

    def test_present_is_parsed(self):
        spec = dict(self.BASE)
        spec["declared_cases"] = [{"case": "a", "red_at_parent": True}]
        gate = sch._lane_gate(self._actor(spec), "lane-x")
        self.assertEqual(gate.declared_cases, (("a", True),))

    def test_malformed_refuses_rather_than_defaulting_to_empty(self):
        spec = dict(self.BASE)
        spec["declared_cases"] = [{"case": "a"}]
        with self.assertRaises(sch.DraftCollectionRefused):
            sch._lane_gate(self._actor(spec), "lane-x")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
