"""A build lane that stops clearing errors blocks for the operator.

Convergence means fewer errors. The history is failed + errored per review
round, so lower is better. Under the grace window a lane may oscillate; at or
past it, every round has to set a strict new low or the lane stops.

Errors rather than passes because a round that collects a different number of
cases makes a pass count incomparable to the one before it -- a suite that
shrinks would read as a regression. The error count stays honest.
"""

import unittest

import adw_modules.scheduler_types as st
from adw_modules.scheduler import _stalled


class StalledPredicateTest(unittest.TestCase):
    def test_flat_errors_stop_on_the_third_round(self) -> None:
        self.assertFalse(_stalled([8]))
        self.assertFalse(_stalled([8, 8]))
        self.assertTrue(_stalled([8, 8, 8]))

    def test_oscillation_stops_when_it_fails_to_beat_the_low(self) -> None:
        # 9,8,10 is still inside the slack; the 9 never beats the 8 reached.
        self.assertTrue(_stalled([9, 8, 10]))
        self.assertTrue(_stalled([9, 8, 10, 9]))

    def test_errors_falling_every_round_never_stops(self) -> None:
        self.assertFalse(_stalled([10, 8, 6]))
        self.assertFalse(_stalled([10, 8, 6, 4]))
        self.assertFalse(_stalled([11, 10, 9, 8, 7, 6, 5]))

    def test_errors_climbing_stops(self) -> None:
        # Getting worse is the clearest case there is.
        self.assertTrue(_stalled([1, 3, 5]))
        self.assertTrue(_stalled([1, 3, 5, 7]))

    def test_one_flat_round_after_the_window_stops(self) -> None:
        self.assertTrue(_stalled([10, 8, 6, 6]))

    def test_a_new_low_on_the_latest_round_never_stops(self) -> None:
        self.assertFalse(_stalled([8, 8, 7]))
        self.assertFalse(_stalled([9, 8, 10, 7]))

    def test_a_clean_suite_still_counts_as_progress(self) -> None:
        self.assertFalse(_stalled([5, 3, 0]))

    def test_short_history_is_never_stalled(self) -> None:
        for history in ([], [0], [5, 5]):
            self.assertFalse(_stalled(history), history)

    def test_the_live_run_history_would_have_stopped_at_round_three(self) -> None:
        # lane-wp7-build ran 11 executed with 10,8,8,8,8,8,8,8 failing and
        # burned every round of it.
        observed = [10, 8, 8, 8, 8, 8, 8, 8]
        self.assertFalse(_stalled(observed[:2]))
        self.assertTrue(_stalled(observed[:3]))

    def test_the_gateway_lane_history_would_have_stopped_at_round_three(
        self,
    ) -> None:
        # lane-wp7-gateway-build never moved off 5 failing across 8 rounds.
        self.assertTrue(_stalled([5, 5, 5]))

    def test_grace_window_is_the_declared_constant(self) -> None:
        self.assertEqual(st.NO_PROGRESS_GRACE_ROUNDS, 3)
        short = [0] * (st.NO_PROGRESS_GRACE_ROUNDS - 1)
        self.assertFalse(_stalled(short))
        self.assertTrue(_stalled(short + [0]))


class WaitReasonTest(unittest.TestCase):
    def test_no_progress_is_resumable_like_a_pause(self) -> None:
        self.assertIn(st.WaitReason.NO_PROGRESS, st.RESUMABLE_WAIT_REASONS)
        self.assertIn(st.WaitReason.PAUSE, st.RESUMABLE_WAIT_REASONS)

    def test_amendment_required_is_not_resumable_by_a_plain_resume(self) -> None:
        self.assertNotIn(
            st.WaitReason.AMENDMENT_REQUIRED, st.RESUMABLE_WAIT_REASONS
        )

    def test_a_blocked_lane_stays_waiting_across_an_unchanged_amendment(
        self,
    ) -> None:
        for reason in st.RESUMABLE_WAIT_REASONS:
            self.assertIs(
                st.amendment_reset_stage(
                    st.LaneStage.BUILDING, changed=False, wait_reason=reason
                ),
                st.LaneStage.WAITING_FOR_USER,
                reason,
            )


if __name__ == "__main__":
    unittest.main()
