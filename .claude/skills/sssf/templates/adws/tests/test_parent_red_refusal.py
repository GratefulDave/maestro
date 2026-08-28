"""A parent-red refusal names the measurement, not the agent.

`adjudicate_parent_red` used to answer two arithmetically different questions
with one code. `TESTS_NO_NEW_CASES` meant either "the tester wrote no new
case" — its real meaning — or "collection found N ids, the run was handed
those N ids, and the report accounted for fewer than N", which is a statement
about the harness that no edit to the tests can satisfy.

Every production occurrence of the second branch was the harness's fault, and
there were three of them on one lane, each hiding the next:

* `run-8a200af7f9044ce7a11a51b6908f37e3` — `_prove_tests_red_at_parent`
  collected a `.test.ts` file with pytest, which collects nothing from it
  (`407d7d3`);
* `run-6b8f607d89744eeb94a79713b3b5d234` — `vitest list --json <path>` was
  read by vitest as "write the listing to <path>", so collection OVERWROTE
  the tester's committed test file and returned zero ids (`5273342`);
* `run-9f20c17ffc22497b957bd5be95dc1ddf` — `vitest list --json` names a case
  `Suite > title` while `--reporter=json` joined the same parts with a plain
  space, and `VitestCaseRunner.run` keeps only outcomes whose id is in the
  collected set, so `set(collected) & set(reported)` was empty by
  construction: nine collected, nine executed, nine red, zero kept
  (`c087469`).

The refusal every one of them produced said the tester had written nothing.
The third cost a whole run to find. Had it said `collected 9, reported
outcomes for 0 of them` beside the command that printed the report, it would
have cost seconds — which is the §1.2 / B15 obligation this suite pins: a
verdict states what was measured, and a number with no reader on the side
that can act is the same defect as no number at all.

So the two facts are two codes, the harness one carries both counts and the
command, and it is ENVIRONMENTAL rather than SEMANTIC: re-prompting a tester
cannot repair a report that dropped its cases, and spending the §7.5 fix
budget to discover that is how `lane-wp6-tests` reached
`NODE_BUDGET_EXHAUSTED_ACROSS_RUNS` across three runs.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


#: The nine ids of `run-9f20c17ffc22497b957bd5be95dc1ddf`, in the shape
#: `vitest list --json` prints them. The ` > ` is the whole incident: the
#: report's `fullName` joined the same parts with a plain space.
VITEST_IDS = tuple(
    "src/geo.test.ts::WP6 entity FAQ block > case {0}".format(index)
    for index in range(1, 10)
)


def _parent_run(
    *,
    collected: int,
    passed: int = 0,
    failed: int = 0,
    errored: int = 0,
    skipped: int = 0,
    nodeids=VITEST_IDS,
    command=("npx", "--no-install", "vitest", "run", "--reporter=json"),
) -> wt.GateResult:
    """A parent run in the shape `run_cases_for` produces one."""
    return wt.GateResult(
        label="parent-red",
        scope="node",
        selector=" ".join(nodeids),
        command=tuple(command),
        exit_code=1,
        green=False,
        counts={
            "collected": collected,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "errored": errored,
        },
    )


class TheTwoFactsAreTwoCodes(unittest.TestCase):
    """The split itself: one arithmetic per refusal code."""

    def test_no_new_case_still_convicts_the_tester(self) -> None:
        """`new_case_count < 1` is unchanged — nothing new was written."""
        verdict = tc.adjudicate_parent_red(
            _parent_run(collected=0, nodeids=()), 0
        )
        self.assertFalse(verdict.verified)
        self.assertEqual(
            tc.TestsRefusal.NO_NEW_CASES.value, verdict.refusal_code,
            "a tester that collected no new nodeid is the one case this code "
            "was ever about; got {0!r}".format(verdict.reason),
        )

    def test_an_unaccounted_parent_run_no_longer_convicts_the_tester(
        self,
    ) -> None:
        """Nine collected, zero accounted for — the c087469 shape.

        This is the assertion the whole split exists for: the tester wrote
        nine cases and the refusal must not be the one that says it wrote
        none.
        """
        verdict = tc.adjudicate_parent_red(_parent_run(collected=0), 9)
        self.assertFalse(verdict.verified)
        self.assertEqual(
            tc.TestsRefusal.PARENT_RUN_UNACCOUNTED.value, verdict.refusal_code,
            "collection found 9 ids and the run accounted for 0 of them; "
            "that is a measurement fault and must not be reported as "
            "{0}. reason={1!r}".format(
                tc.TestsRefusal.NO_NEW_CASES.value, verdict.reason),
        )
        self.assertNotIn(
            tc.TestsRefusal.NO_NEW_CASES.value, verdict.reason or "",
            "the old code must not survive anywhere in the reason, or a "
            "grep over the ledger still finds the accusation",
        )

    def test_a_partial_shortfall_is_the_same_measurement_fault(self) -> None:
        """Eight of nine accounted for is the same defect, smaller."""
        verdict = tc.adjudicate_parent_red(_parent_run(collected=8), 9)
        self.assertEqual(
            tc.TestsRefusal.PARENT_RUN_UNACCOUNTED.value, verdict.refusal_code
        )

    def test_the_two_codes_are_distinct_members(self) -> None:
        """A split that reused the value would pass every test above and
        change nothing an operator or `refusal_repetition` can read."""
        self.assertNotEqual(
            tc.TestsRefusal.NO_NEW_CASES.value,
            tc.TestsRefusal.PARENT_RUN_UNACCOUNTED.value,
        )

    def test_the_new_member_declares_a_remedy_and_stamps_it(self) -> None:
        verdict = tc.adjudicate_parent_red(_parent_run(collected=0), 9)
        self.assertEqual(
            tc.TestsRefusal.PARENT_RUN_UNACCOUNTED.remedy, verdict.remedy
        )
        self.assertIn(
            "names the harness", tc.TestsRefusal.PARENT_RUN_UNACCOUNTED.remedy,
            "the remedy must tell the reader the tests are not the subject, "
            "the way COLLECTION_FAILED's does",
        )


class TheDetailCarriesBothSides(unittest.TestCase):
    """The numbers that would have ended the third incident in seconds."""

    def test_both_counts_and_the_shortfall_are_in_the_detail(self) -> None:
        reason = tc.adjudicate_parent_red(_parent_run(collected=8), 9).reason
        assert reason is not None
        for fragment in ("9 new case(s)", "8 of them", "1 unaccounted"):
            self.assertIn(
                fragment, reason,
                "an operator reading transitions.detail_json must be able to "
                "tell which side is wrong without re-running anything; "
                "{0!r} is missing from {1!r}".format(fragment, reason),
            )

    def test_an_empty_intersection_reads_differently_from_a_near_miss(
        self,
    ) -> None:
        """`reported 0 of 9` and `reported 8 of 9` are different diagnoses.

        Zero means the two id surfaces never met — bugs 1, 2 and 3 all landed
        here. A shortfall of one means the run lost a case it did name, which
        is a different search.
        """
        empty = tc.adjudicate_parent_red(_parent_run(collected=0), 9).reason
        near = tc.adjudicate_parent_red(_parent_run(collected=8), 9).reason
        assert empty is not None and near is not None
        self.assertNotEqual(
            empty, near,
            "two different measurements produced one byte-identical string, "
            "which is the non-convergence shape refusal_repetition exists to "
            "stop and the operator has no way to tell apart",
        )
        self.assertIn("intersect nowhere", empty)
        self.assertNotIn(
            "intersect nowhere", near,
            "8 of 9 reported means the surfaces did meet; claiming otherwise "
            "sends the reader hunting the wrong defect",
        )

    def test_the_detail_names_the_command_that_produced_the_report(
        self,
    ) -> None:
        """`vitest run --reporter=json` in the verdict is the whole of bug 3.

        The command is already on the `GateResult` every caller builds; not
        printing it is what left three runs guessing which runner had been
        asked, and once for a `.test.ts` file the answer was pytest.
        """
        reason = tc.adjudicate_parent_red(_parent_run(collected=0), 9).reason
        assert reason is not None
        self.assertIn("vitest run --reporter=json", reason)

    def test_the_collected_ids_are_carried_so_their_shape_is_visible(
        self,
    ) -> None:
        """The ` > ` separator is only recognisable when it is printed."""
        reason = tc.adjudicate_parent_red(
            _parent_run(collected=0, nodeids=VITEST_IDS[:2]), 2
        ).reason
        assert reason is not None
        self.assertIn("WP6 entity FAQ block > case 1", reason)

    def test_a_long_id_list_is_elided_with_the_loss_stated(self) -> None:
        """Bounded, because one refusal must not flood the ledger row — and
        stated, because a detail that stops mid-id reads as an id that stops
        there, which is the same confusion this verdict exists to prevent."""
        long_ids = tuple(
            "src/geo.test.ts::WP6 entity FAQ block > case {0}".format(index)
            for index in range(200)
        )
        reason = tc.adjudicate_parent_red(
            _parent_run(collected=0, nodeids=long_ids), 200
        ).reason
        assert reason is not None
        self.assertIn("more characters", reason)
        self.assertLess(
            len(reason), 1200,
            "the detail is a ledger field, not a transcript: {0} characters"
            .format(len(reason)),
        )


class TheRetryClassMatchesWhoCanFixIt(unittest.TestCase):
    """§7.5: a SEMANTIC retry re-prompts the agent. This is not fixable there.

    `mutates_prompt` is true only for SEMANTIC, so classifying a measurement
    fault SEMANTIC does three wrong things at once: it hands the tester a
    verdict it cannot act on, it decrements the fix loop's budget, and it
    leaves the node to exhaust that budget and block as though the *content*
    had failed. ENVIRONMENTAL draws on `environmental_retries` instead and
    blocks as `ENVIRONMENTAL_BUDGET_EXHAUSTED`, which names the machine —
    the same classification `COLLECTION_FAILED` already carries for the
    neighbouring "the runner produced no parseable report" fact.
    """

    def test_an_unaccounted_run_is_environmental(self) -> None:
        verdict = tc.adjudicate_parent_red(_parent_run(collected=0), 9)
        self.assertEqual(st.RetryClass.ENVIRONMENTAL, verdict.retry_class)

    def test_an_unaccounted_run_does_not_rewrite_the_agent_prompt(
        self,
    ) -> None:
        """The consequence, asserted through the predicate that decides it."""
        verdict = tc.adjudicate_parent_red(_parent_run(collected=0), 9)
        assert verdict.retry_class is not None
        self.assertFalse(
            st.mutates_prompt(verdict.retry_class),
            "re-prompting a tester with a harness measurement fault is how "
            "lane-wp6-tests spent four attempts across three runs",
        )

    def test_no_new_cases_stays_semantic(self) -> None:
        """The tester's half is unchanged: it can, and must, write a case."""
        verdict = tc.adjudicate_parent_red(
            _parent_run(collected=0, nodeids=()), 0
        )
        self.assertEqual(st.RetryClass.SEMANTIC, verdict.retry_class)
        assert verdict.retry_class is not None
        self.assertTrue(st.mutates_prompt(verdict.retry_class))


class TheNeighbouringClausesAreUnmoved(unittest.TestCase):
    """The split must not swallow a clause that was already correct."""

    def test_every_new_case_red_still_verifies(self) -> None:
        verdict = tc.adjudicate_parent_red(
            _parent_run(collected=9, failed=9), 9
        )
        self.assertTrue(verdict.verified, verdict.reason)

    def test_a_green_case_is_still_hollow_not_unaccounted(self) -> None:
        verdict = tc.adjudicate_parent_red(
            _parent_run(collected=9, passed=1, failed=8), 9
        )
        self.assertEqual(
            tc.TestsRefusal.HOLLOW_AT_PARENT.value, verdict.refusal_code
        )

    def test_an_import_crash_is_still_an_import_crash(self) -> None:
        verdict = tc.adjudicate_parent_red(
            _parent_run(collected=9, failed=8, errored=1), 9
        )
        self.assertEqual(
            tc.TestsRefusal.IMPORT_CRASH.value, verdict.refusal_code
        )

    def test_no_report_at_all_is_still_collection_failed(self) -> None:
        """Empty counts are "no report", never "zero cases" — the shortfall
        branch must not claim a run that never happened."""
        result = wt.GateResult(
            label="parent-red", scope="node", selector=" ".join(VITEST_IDS),
            command=("vitest", "run", "--reporter=json"),
            exit_code=1, green=False, counts={})
        verdict = tc.adjudicate_parent_red(result, 9)
        self.assertEqual(
            tc.TestsRefusal.COLLECTION_FAILED.value, verdict.refusal_code
        )

    def test_a_run_that_lost_a_case_after_it_ran_is_still_not_red(
        self,
    ) -> None:
        """`collected == new_case_count` but not every case failed: that is
        NOT_RED_AT_PARENT, which is about the tests and stays SEMANTIC."""
        verdict = tc.adjudicate_parent_red(
            _parent_run(collected=9, failed=7, skipped=2), 9
        )
        self.assertEqual(
            tc.TestsRefusal.NOT_RED_AT_PARENT.value, verdict.refusal_code
        )
        self.assertEqual(st.RetryClass.SEMANTIC, verdict.retry_class)


class _DisagreeingRunner(tc.CaseRunner):
    """A runner whose report names its cases differently from its listing.

    Not a hypothetical: this is `VitestCaseRunner` before `c087469`. `collect`
    returns `Suite > title`; `run` executes everything, is handed the
    collected ids, and keeps only outcomes whose id is in that set — so a
    report using a different join keeps nothing, prints no complaint, and
    exits with the suite's own code.
    """

    name = "vitest"

    def __init__(self, collected, reported_join: str) -> None:
        self._collected = tuple(collected)
        self._join = reported_join

    def collect(self, tree, paths, timeout_s: float = 120.0):
        return self._collected

    def run(self, tree, nodeids, timeout_s: float = 300.0) -> tc.CaseRun:
        reported = tuple(
            tc.CaseOutcome(
                nodeid=nodeid.replace(" > ", self._join),
                status="failed",
                reason="AssertionError: the implementation is absent",
            )
            for nodeid in self._collected
        )
        wanted = set(nodeids)
        return tc.CaseRun(
            outcomes=tuple(o for o in reported if o.nodeid in wanted),
            exit_code=1,
            collection_failed=False,
            command=("vitest", "run", "--reporter=json"),
        )


class ThroughRunCasesForEndToEnd(unittest.TestCase):
    """The path the scheduler actually takes, with no hand-built counts."""

    def test_the_c087469_shape_refuses_the_harness_not_the_tester(
        self,
    ) -> None:
        runner = _DisagreeingRunner(VITEST_IDS, reported_join=" ")
        result = tc.run_cases_for(runner, Path("."), VITEST_IDS)
        self.assertEqual(0, result.counts["collected"])
        verdict = tc.adjudicate_parent_red(result, len(VITEST_IDS))
        self.assertEqual(
            tc.TestsRefusal.PARENT_RUN_UNACCOUNTED.value, verdict.refusal_code,
            "nine cases ran and failed for the right reason; the refusal "
            "must name the report that dropped them. reason={0!r}".format(
                verdict.reason),
        )
        self.assertEqual(st.RetryClass.ENVIRONMENTAL, verdict.retry_class)

    def test_agreeing_id_surfaces_verify(self) -> None:
        """The control: the same runner, the join fixed, is the red witness."""
        runner = _DisagreeingRunner(VITEST_IDS, reported_join=" > ")
        result = tc.run_cases_for(runner, Path("."), VITEST_IDS)
        verdict = tc.adjudicate_parent_red(result, len(VITEST_IDS))
        self.assertTrue(verdict.verified, verdict.reason)


if __name__ == "__main__":
    unittest.main()
