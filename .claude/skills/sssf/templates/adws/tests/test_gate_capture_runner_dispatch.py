"""The capture check reads a candidate in the language it is written in.

`run-9d03105407f440079f3730f1fe4c67b3` blocked its build lane with

    ENVIRONMENTAL_BUDGET_EXHAUSTED
    TEST_PAIRING_TEST_TREE_UNREADABLE: GATE_CAPTURE_ACCEPTED_TESTS_UNPARSEABLE:
    the accepted test candidate does not parse
    (unterminated string literal (detected at line 118))

`scheduler` resolved the collector from the accepted candidate's runner --
`tc.case_runner(accepted.runner)` -- and then handed the accepted bytes to
`gate_capture`, which parsed them with CPython's `ast.parse`. The candidate
was TypeScript. Line 118 read

    * A truncated response is the analytics contract's own way of saying …

and Python read that apostrophe as the start of a string literal.

The refusal's own remedy says "this names the machine, not the diff", so it
is classified ENVIRONMENTAL and retried unchanged. Three retries exhausted
the node's budget. No vitest build lane could ever have passed it, and the
lane that hit it was the first in this project to drive a vitest gate to the
pairing check at all.

The parse is the same shape as the vitest watch-mode hang fixed the same day:
a harness step that assumes pytest and dispatches on nothing.
"""

import unittest

from adw_modules import gate_capture as gc
from adw_modules import tests_chain as tc


#: The exact hazard, in the exact position: an apostrophe inside a block
#: comment, ahead of every case. Python tokenises it as an unterminated
#: string; TypeScript does not care.
TS_SOURCE = '''import { describe, it, expect } from "vitest";

/**
 * A truncated response is the analytics contract's own way of saying the
 * returned rows are incomplete.
 */
describe("WP6 GEO entity page", () => {
  it("carries at most five FAQ pairs", () => { expect(1).toBe(1); });
  it('omits a question whose data is absent', () => { expect(1).toBe(1); });
  test(`carries the release identifier and its unit`, () => {});
  it.skip("is skipped but still declared", () => {});
  it.each([[1], [2]])("row %i is parametrised", () => {});
  it(`interpolates ${"a"} into its title`, () => {});
});
'''

PY_SOURCE = '''"""A docstring with an apostrophe's worth of trouble."""
import pytest


def test_alpha():
    assert True


@pytest.mark.parametrize("n", [1, 2])
def test_beta(n):
    assert n
'''


class VitestReadsItsOwnLanguage(unittest.TestCase):

    def setUp(self) -> None:
        self.runner = tc.VitestCaseRunner()

    def test_the_apostrophe_that_blocked_the_run_is_not_a_string(self) -> None:
        names = self.runner.defined_case_names(TS_SOURCE)
        self.assertIn("carries at most five FAQ pairs", names)

    def test_every_quote_style_declares_a_case(self) -> None:
        names = self.runner.defined_case_names(TS_SOURCE)
        self.assertIn("omits a question whose data is absent", names)
        self.assertIn("carries the release identifier and its unit", names)

    def test_a_modifier_does_not_hide_a_case(self) -> None:
        self.assertIn("is skipped but still declared",
                      self.runner.defined_case_names(TS_SOURCE))

    def test_each_and_interpolation_are_parametrised(self) -> None:
        parametrised = self.runner.parametrised_case_names(TS_SOURCE)
        self.assertIn("row %i is parametrised", parametrised)
        self.assertIn('interpolates ${"a"} into its title', parametrised)
        self.assertNotIn("carries at most five FAQ pairs", parametrised)

    def test_the_case_name_is_the_last_suite_segment(self) -> None:
        self.assertEqual(
            self.runner.case_name_of(
                "src/a.test.ts::Outer > Inner > does the thing"),
            "does the thing",
        )


class TheRealCandidateThatBlockedTheRun(unittest.TestCase):
    """The production file, read as vitest itself read it.

    Vitest collected exactly eight cases from this candidate. The extractor
    must find the same eight -- not seven, not nine -- because every name it
    misses becomes a stray and refuses an honest lane.
    """

    SOURCE = (
        'import { describe, it, expect } from "vitest";\n'
        '\n'
        '/**\n'
        " * A truncated response is the analytics contract's own way of saying"
        ' the returned\n'
        ' * rows are incomplete.\n'
        ' */\n'
        'describe("WP6 GEO entity page", () => {\n'
        '  it("carries at most five FAQ pairs", () => {});\n'
        '  it("enforces the 320-character answer cap", () => {});\n'
        '  it("omits a question whose data is absent", () => {});\n'
        '  it("indexes an entity that meets the report threshold", () => {});\n'
        '  it("refuses indexation when the report count falls below the '
        'report threshold", () => {});\n'
        '  it("refuses indexation when the entity is absent from the active '
        'release", () => {});\n'
        '  it("refuses indexation when a trend sub-page inherits its parent '
        'decision", () => {});\n'
        '  it("carries the release identifier and its unit", () => {});\n'
        '});\n'
    )

    def test_all_eight_cases_are_found(self) -> None:
        names = tc.VitestCaseRunner().defined_case_names(self.SOURCE)
        self.assertEqual(len(names), 8, sorted(names))

    def test_the_collected_ids_produce_no_strays(self) -> None:
        runner = tc.VitestCaseRunner()
        reported = tuple(
            "src/lib/seo/geo-entity-page.test.ts::WP6 GEO entity page > "
            + name
            for name in runner.defined_case_names(self.SOURCE)
        )
        self.assertEqual(
            gc.unexpected_cases(self.SOURCE, reported, runner), (),
            "the pairing check must accept the candidate vitest accepted",
        )

    def test_it_still_refuses_under_the_python_reader(self) -> None:
        """The defect itself, so the cases above cannot pass vacuously."""
        with self.assertRaises(gc.GateCaptureRefusal) as caught:
            gc.unexpected_cases(self.SOURCE, ())
        self.assertIn("does not parse", str(caught.exception))


class PytestArmIsUnchanged(unittest.TestCase):
    """The default path must be byte-for-byte what it was."""

    def test_no_runner_still_means_the_python_reader(self) -> None:
        self.assertEqual(
            gc.unexpected_cases(PY_SOURCE, ("tests/t.py::test_alpha",)), ())

    def test_the_pytest_runner_agrees_with_the_default(self) -> None:
        runner = tc.PytestCaseRunner()
        self.assertEqual(
            runner.defined_case_names(PY_SOURCE),
            gc.case_names_defined(PY_SOURCE))
        self.assertEqual(
            runner.parametrised_case_names(PY_SOURCE),
            gc.parametrised_case_names(PY_SOURCE))
        self.assertEqual(
            runner.case_name_of("tests/t.py::TestX::test_beta[1]"), "test_beta")


class TheCheckStillCatchesAForgery(unittest.TestCase):
    """Dispatching must not soften what the guard exists to refuse."""

    def test_a_reported_case_the_source_does_not_declare_is_a_stray(
            self) -> None:
        strays = gc.unexpected_cases(
            TS_SOURCE,
            ("a.test.ts::WP6 GEO entity page > carries at most five FAQ pairs",
             "a.test.ts::WP6 GEO entity page > invented at collection time"),
            tc.VitestCaseRunner())
        self.assertEqual(
            strays,
            ("a.test.ts::WP6 GEO entity page > invented at collection time",))

    def test_min_cases_above_what_the_source_declares_is_a_shortfall(
            self) -> None:
        self.assertEqual(
            gc.unsatisfiable_min_cases(TS_SOURCE, 99, tc.VitestCaseRunner()),
            99 - len(tc.VitestCaseRunner().defined_case_names(TS_SOURCE)))

    def test_a_typescript_candidate_no_longer_refuses_as_unreadable(
            self) -> None:
        with self.assertRaises(gc.GateCaptureRefusal):
            gc.unexpected_cases(TS_SOURCE, ("a.test.ts::x",))
        # …and with the runner it reads cleanly, which is the whole repair.
        gc.unexpected_cases(
            TS_SOURCE,
            ("a.test.ts::WP6 GEO entity page > carries at most five FAQ pairs",),
            tc.VitestCaseRunner())


class ARunnerThatCannotReadIsRefusedByName(unittest.TestCase):

    def test_a_runner_missing_the_surface_refuses_rather_than_crashing(
            self) -> None:
        class Half:
            name = "half"

            def defined_case_names(self, source):
                return frozenset()

        with self.assertRaises(gc.GateCaptureRefusal) as caught:
            gc.unexpected_cases(TS_SOURCE, (), Half())
        self.assertIn("half", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
