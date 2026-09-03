"""The totals line is the one carrying counts, not the last line shaped like it.

`rr.execute_cases` returns `stdout + "\\n" + stderr`, so anything vitest writes
to stderr lands after the summary regardless of print order, and the parser
scans backwards. Two stderr lines have now been mistaken for the totals line:
the failure banner, which the shape anchor fixed, and

    Tests closed successfully but something prevents Vite server from exiting

which has exactly that shape. Measured 2026-09-03 on FDAdb
`lane-wp7-page-build`, whose sealed config boots the Astro pipeline and leaves
the Vite server open: 15 cases ran, the runner exited 0, and the sealed suite
refused `SEALED_SUITE_COUNTS_UNPARSEABLE` against a candidate whose tests all
passed.

These are real captures, split into the two streams the harness captures
separately. Running vitest with `2>&1` interleaves them and hides the defect,
which is why a shell measurement never showed it.
"""

from __future__ import annotations

import unittest

from adw_modules import tests_chain as tc

PASSING_STDOUT = """
 RUN  v3.2.7 /tmp/review-lane-wp7-page-build

 ✓ tests/wp7/entity-route.test.ts (8 tests) 16ms
 ✓ tests/wp7/entity-route.render.test.ts (7 tests) 28ms

 Test Files  2 passed (2)
      Tests  15 passed (15)
   Start at  05:07:22
   Duration  1.25s
""".strip("\n")

HANGING_SERVER_STDERR = """
close timed out after 10000ms
Tests closed successfully but something prevents Vite server from exiting
You can try to identify the cause by enabling "hanging-process" reporter.
""".strip("\n")

FAILING_STDOUT = """
 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 1 passed (2)
""".strip("\n")

FAILURE_BANNER_STDERR = "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯"


def _combined(stdout: str, stderr: str) -> str:
    return stdout + "\n" + stderr


class VitestTotals(unittest.TestCase):
    def test_a_passing_suite_survives_the_hanging_server_warning(self) -> None:
        counts = tc._parse_suite_counts(
            "vitest", _combined(PASSING_STDOUT, HANGING_SERVER_STDERR)
        )
        self.assertEqual(counts["passed"], 15)
        self.assertEqual(counts["failed"], 0)

    def test_the_warning_alone_parses_nothing(self) -> None:
        # A run that produced no totals line still refuses. The check exists
        # for exactly that, and this must not become a way to pass without one.
        counts = tc._parse_suite_counts("vitest", HANGING_SERVER_STDERR)
        self.assertEqual(sum(counts.values()), 0)

    def test_a_failing_suite_survives_the_failure_banner(self) -> None:
        counts = tc._parse_suite_counts(
            "vitest", _combined(FAILING_STDOUT, FAILURE_BANNER_STDERR)
        )
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["passed"], 1)

    def test_both_stderr_lines_together_still_leave_the_totals_readable(self) -> None:
        counts = tc._parse_suite_counts(
            "vitest",
            _combined(
                PASSING_STDOUT, HANGING_SERVER_STDERR + "\n" + FAILURE_BANNER_STDERR
            ),
        )
        self.assertEqual(counts["passed"], 15)

    def test_test_files_line_is_not_the_totals_line(self) -> None:
        # ` Test Files  2 passed (2)` carries a count and sits before the real
        # totals line; the word there is `Test`, so it must not match.
        self.assertFalse(tc._VITEST_SUMMARY.match(" Test Files  2 passed (2)"))

    def test_the_summary_line_matches(self) -> None:
        self.assertTrue(tc._VITEST_SUMMARY.match("      Tests  15 passed (15)"))
        self.assertTrue(
            tc._VITEST_SUMMARY.match("      Tests  1 failed | 1 passed (2)")
        )

    def test_the_hanging_warning_does_not_match(self) -> None:
        self.assertFalse(
            tc._VITEST_SUMMARY.match(
                "Tests closed successfully but something prevents Vite server "
                "from exiting"
            )
        )


if __name__ == "__main__":
    unittest.main()
