"""`adw_modules/gate_capture.py`'s refusals are executed against real bytes.

The module's own docstring records what it was built for: on 2026-08-27
`lane-routing-chemical` was honestly refused `2 passed, min_cases is 5` and
answered by putting ~190 lines of collection-time apparatus into production
source to manufacture the other three cases. Six mechanism families, and every
existing guard acquitted it, because each was asked a question the forgery did
not fail.

The subject is still live. `code_review.py` still adjudicates a sealed run on
`run["executed"] < min_cases` -- a count the runner reports, produced by a
process the code under test runs inside -- which is the defect class named in
the first line of `gate_capture.py`. What is *not* live is any caller: the
artifact-factory cutover (`e7b477e`) deleted `tests/test_gate_capture.py`,
`tests/test_gate_capture_runner_dispatch.py` and
`tests/test_min_cases_enforcement.py` together, and from that day nothing in
the runtime imported the module. Three hundred lines of provenance check,
never executed.

This file restores execution. It does not wire the check into adjudication:
making a reviewer's verdict depend on `unexpected_cases` is a decision about a
running factory and belongs to whoever owns that contract, not to a repair that
was asked to resolve an orphan. What it does establish is that the refusals
still work on today's bytes, so that wiring is a one-line change rather than an
archaeology project.

One stale reference is worth recording rather than repairing: the `_reader`
docstring says extraction "moved onto `tests_chain.CaseRunner`", and no
`CaseRunner` exists anywhere in this copy any more. The dispatch itself is
duck-typed and works with any object carrying the three methods -- the case
below proves it -- so only the pointer is out of date.
"""

from __future__ import annotations

import unittest

from adw_modules import gate_capture as gc

ACCEPTED = '''\
"""Two cases, which is what the accepted candidate defined."""


def test_alpha():
    assert True


async def test_beta():
    assert True


class TestGamma:
    def test_inner(self):
        assert True

    def helper(self):
        return None


def not_a_test():
    return None
'''

PARAMETRISED = """\
import pytest


@pytest.mark.parametrize("value", [1, 2, 3])
def test_over_values(value):
    assert value


def test_plain():
    assert True
"""


class NamesComeFromTheBytes(unittest.TestCase):
    def test_defined_names_are_read_without_running_anything(self) -> None:
        self.assertEqual(
            frozenset({"test_alpha", "test_beta", "test_inner"}),
            gc.case_names_defined(ACCEPTED),
        )

    def test_a_helper_inside_a_test_class_is_not_a_case(self) -> None:
        self.assertNotIn("helper", gc.case_names_defined(ACCEPTED))
        self.assertNotIn("not_a_test", gc.case_names_defined(ACCEPTED))

    def test_a_node_id_yields_the_authored_case_name(self) -> None:
        self.assertEqual("test_y", gc.case_name_of("tests/t.py::TestX::test_y[3]"))
        self.assertEqual("test_alpha", gc.case_name_of("tests/t.py::test_alpha"))

    def test_a_node_id_with_no_case_name_is_refused(self) -> None:
        with self.assertRaises(gc.GateCaptureRefusal) as caught:
            gc.case_name_of("tests/t.py::")
        self.assertIn(gc.NODEID_UNPARSEABLE, str(caught.exception))

    def test_parametrised_names_are_the_decorated_ones_only(self) -> None:
        self.assertEqual(
            frozenset({"test_over_values"}),
            gc.parametrised_case_names(PARAMETRISED),
        )


class ThreeCaptureRoutesAreRefused(unittest.TestCase):
    """Each of the shapes the docstring enumerates, on real node ids."""

    def test_an_honest_collection_is_accepted(self) -> None:
        self.assertEqual(
            (),
            gc.unexpected_cases(
                ACCEPTED, ["t.py::test_alpha", "t.py::TestGamma::test_inner"]
            ),
        )

    def test_a_name_the_accepted_file_does_not_define(self) -> None:
        strays = gc.unexpected_cases(
            ACCEPTED, ["t.py::test_alpha", "t.py::test_manufactured"]
        )
        self.assertEqual(("t.py::test_manufactured",), strays)

    def test_a_repeated_node_id_inflates_a_count_and_is_refused(self) -> None:
        strays = gc.unexpected_cases(
            ACCEPTED, ["t.py::test_alpha"] * 3
        )
        self.assertEqual(("t.py::test_alpha", "t.py::test_alpha"), strays)

    def test_a_bracketed_id_is_refused_even_for_a_defined_name(self) -> None:
        strays = gc.unexpected_cases(ACCEPTED, ["t.py::test_alpha[0]"])
        self.assertEqual(("t.py::test_alpha[0]",), strays)

    def test_a_genuine_parametrisation_is_refused_too(self) -> None:
        """The stance the module argues for, asserted so a relaxation is loud.

        `test_over_values` really is decorated, and its instances are still
        refused, because the number a genuine `parametrize` yields is not
        statically bounded.
        """
        strays = gc.unexpected_cases(
            PARAMETRISED,
            ["t.py::test_over_values[{}]".format(i) for i in range(3)],
        )
        self.assertEqual(3, len(strays))

    def test_unparseable_accepted_bytes_refuse_rather_than_admit(self) -> None:
        with self.assertRaises(gc.GateCaptureRefusal) as caught:
            gc.unexpected_cases("def test_a(:\n", ["t.py::test_a"])
        self.assertIn(gc.ACCEPTED_TESTS_UNPARSEABLE, str(caught.exception))


class AnUnsatisfiableGate(unittest.TestCase):
    """The root cause: a gate no honest attempt can ever pass."""

    def test_the_shortfall_is_the_cases_the_file_cannot_supply(self) -> None:
        self.assertEqual(2, gc.unsatisfiable_min_cases(ACCEPTED, 5))

    def test_a_satisfiable_gate_reports_no_shortfall(self) -> None:
        self.assertEqual(0, gc.unsatisfiable_min_cases(ACCEPTED, 3))
        self.assertEqual(0, gc.unsatisfiable_min_cases(ACCEPTED, 1))

    def test_unparseable_accepted_bytes_refuse_here_as_well(self) -> None:
        with self.assertRaises(gc.GateCaptureRefusal):
            gc.unsatisfiable_min_cases("def test_a(:\n", 1)


class _JsCaseReader:
    """A non-pytest reader, duck-typed exactly as `_reader` requires."""

    name = "vitest"

    def defined_case_names(self, source: str) -> frozenset:
        return frozenset(
            line.split("'")[1]
            for line in source.splitlines()
            if line.strip().startswith("it('")
        )

    def parametrised_case_names(self, source: str) -> frozenset:
        return frozenset()

    def case_name_of(self, nodeid: str) -> str:
        return str(nodeid).rsplit(" > ", 1)[-1].strip()


class RunnerDispatch(unittest.TestCase):
    def test_a_foreign_runner_reads_its_own_language(self) -> None:
        source = "describe('x', () => {\n  it('renders', () => {});\n});\n"
        reader = _JsCaseReader()
        self.assertEqual(
            (),
            gc.unexpected_cases(source, ["suite.test.ts > x > renders"], reader),
        )
        self.assertEqual(
            ("suite.test.ts > x > invented",),
            gc.unexpected_cases(source, ["suite.test.ts > x > invented"], reader),
        )

    def test_a_runner_that_cannot_read_its_own_source_is_refused(self) -> None:
        class Halfway:
            name = "halfway"

            def case_name_of(self, nodeid: str) -> str:
                return nodeid

        with self.assertRaises(gc.GateCaptureRefusal) as caught:
            gc.unexpected_cases(ACCEPTED, ["t.py::test_alpha"], Halfway())
        self.assertIn(gc.ACCEPTED_TESTS_UNPARSEABLE, str(caught.exception))

    def test_no_runner_still_means_pytest(self) -> None:
        self.assertEqual(
            gc.unexpected_cases(ACCEPTED, ["t.py::test_alpha"]),
            gc.unexpected_cases(ACCEPTED, ["t.py::test_alpha"], None),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
