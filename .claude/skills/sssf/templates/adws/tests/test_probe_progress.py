"""A build round's progress is a tuple, and a probe discharged does not pay for a loss.

Part one drives `_stalled` with the outcomes `cr.review_progress` returns:
a plain passed count for a review that never carried a probe, and
`(passed, discharged ids, active ids)` for one that did. Part two drives the
same rules through a real lane, where the outcomes are read off real CODE_REVIEW
artifacts by `_review_content_history`.
"""

import unittest

from adw_modules import code_review as cr
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from tests.probe_lane_support import ProbeLaneCase

CODE = st.ArtifactKind.CODE_REVIEW
REVISE = st.ReviewerVerdict.REVISE


def history(*outcomes):
    """Distinct content per round, so only the outcomes can stall the lane."""
    return [("content-{0}".format(i), outcome) for i, outcome in enumerate(outcomes)]


def tup(passed, discharged=(), active=()):
    return (passed, tuple(sorted(discharged)), tuple(sorted(active)))


class StalledRulesTest(unittest.TestCase):
    def test_advancing_suite_counts_do_not_stall(self):
        self.assertFalse(sch._stalled(history(10, 11, 12), CODE))

    def test_flat_suite_counts_stall_as_before(self):
        self.assertTrue(sch._stalled(history(10, 10, 10), CODE))

    def test_prose_only_rounds_with_open_probes_and_no_discharge_still_stall(self):
        self.assertTrue(
            sch._stalled(history(*[tup(10, (), ("A",))] * 3), CODE)
        )

    def test_a_suite_loss_is_a_regression_even_when_a_probe_is_discharged(self):
        # (10, 0) -> (9, 1): the sum is unchanged, the suite lost a case.
        self.assertTrue(
            sch._stalled(history(tup(10, (), ("A",)), tup(9, ("A",), ("A",))), CODE)
        )

    def test_a_probe_lost_while_another_is_gained_is_a_regression(self):
        self.assertTrue(
            sch._stalled(
                history(
                    tup(10, ("A",), ("A", "B")),
                    tup(10, ("B",), ("A", "B")),
                ),
                CODE,
            )
        )

    def test_a_retired_probe_is_not_a_lost_one(self):
        self.assertFalse(
            sch._stalled(history(tup(10, ("A",), ("A",)), tup(10, (), ())), CODE)
        )

    def test_discharging_one_more_probe_each_round_is_progress(self):
        self.assertFalse(
            sch._stalled(
                history(
                    tup(10, (), ("A",)),
                    tup(10, ("A",), ("A", "B")),
                    tup(10, ("A", "B"), ("A", "B", "C")),
                ),
                CODE,
            )
        )

    def test_plain_and_tuple_outcomes_compare_in_one_history(self):
        self.assertFalse(
            sch._stalled(
                history(10, tup(10, (), ("A",)), tup(10, ("A",), ("A", "B"))), CODE
            )
        )
        self.assertTrue(
            sch._stalled(history(tup(11, (), ("A",)), 10), CODE),
            "a plain count below a tuple's passed count regresses",
        )

    def test_review_progress_feeds_stalled_unchanged(self):
        no_probe = {"public_result_summary": {"passed": 10}}
        self.assertEqual(cr.review_progress(no_probe), 10)
        probed = {
            "public_result_summary": {"passed": 10},
            "probe_records": [{"probe_id": "A"}],
            "probe_status": {"A": "DISCHARGED"},
        }
        self.assertEqual(cr.review_progress(probed), (10, ("A",), ("A",)))


class LaneProgressTest(ProbeLaneCase):
    def outcomes(self):
        return [
            outcome
            for _, outcome in sch._review_content_history(
                self.store, self.run_id, self.lane_id, CODE
            )
        ]

    def test_history_reads_tuples_off_real_reviews(self):
        self.start(["8", "8 alpha"], [{"verdict": REVISE, "probes": ["alpha"]}, {"verdict": REVISE}])
        self.round()
        self.round()
        self.assertEqual(len(self.outcomes()), 2)
        first, second = self.outcomes()
        self.assertEqual(first[0], 8)
        self.assertEqual(first[1], ())
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(second[1], first[2], "the probe admitted in round one is discharged in round two")

    def test_a_suite_loss_beside_a_discharge_parks_the_lane(self):
        # round 1: suite 5/8, probe alpha open.  round 2: suite 6/8, alpha
        # discharged, beta open.  round 3: suite 5/8 again, beta discharged.
        self.start(
            ["5", "6 alpha", "5 alpha beta"],
            [
                {"verdict": REVISE, "probes": ["alpha"]},
                {"verdict": REVISE, "probes": ["beta"]},
                {"verdict": REVISE},
            ],
        )
        self.round()
        self.round()
        self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)
        reviews = self.reviews()
        self.assertEqual([r["public_result_summary"]["passed"] for r in reviews], [5, 6, 5])
        self.assertEqual(self.waits()[0]["wait_reason"], st.WaitReason.NO_PROGRESS.value)

    def test_a_probe_lost_beside_a_new_discharge_parks_the_lane(self):
        bodies = ["8", "8 alpha", "8 alpha beta", "8 beta gamma"]
        script = [
            {"verdict": REVISE, "probes": ["alpha"]},
            {"verdict": REVISE, "probes": ["beta"]},
            {"verdict": REVISE, "probes": ["gamma"]},
            {"verdict": REVISE},
        ]
        self.start(bodies, script)
        for _ in range(3):
            self.round()
        self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)
        status = self.reviews()[-1]["probe_status"]
        self.assertEqual(sorted(status.values()), ["DISCHARGED", "DISCHARGED", "OPEN"])

    def test_the_same_lane_without_the_loss_keeps_going(self):
        bodies = ["8", "8 alpha", "8 alpha beta", "8 alpha beta gamma"]
        script = [
            {"verdict": REVISE, "probes": ["alpha"]},
            {"verdict": REVISE, "probes": ["beta"]},
            {"verdict": REVISE, "probes": ["gamma"]},
            {"verdict": REVISE},
        ]
        self.start(bodies, script)
        for _ in range(4):
            self.round()
        self.assertIs(self.stage(), st.LaneStage.BUILDING)


if __name__ == "__main__":
    unittest.main()
