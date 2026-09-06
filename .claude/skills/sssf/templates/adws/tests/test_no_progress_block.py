"""A build lane that stops clearing errors blocks for the operator.

Convergence means fewer errors. The history is failed + errored per review
round, so lower is better. Under the grace window a lane may oscillate; at or
past it, every round has to set a strict new low or the lane stops.

Errors rather than passes because a round that collects a different number of
cases makes a pass count incomparable to the one before it -- a suite that
shrinks would read as a regression. The error count stays honest.
"""

import json
import pathlib
import tempfile
import unittest

import adw_modules.scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.scheduler import (
    _sealed_error_history,
    _stalled,
    _test_review_error_history,
)


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


class RoundWindowTest(unittest.TestCase):
    """The window is the rounds of the argument the lane is having now.

    `_stalled` is exercised above on bare lists. What builds those lists is
    the SQL in `_sealed_error_history` and `_test_review_error_history`, and
    it is the part that was wrong: it counted every round the lane had ever
    had, so a lane an amendment sent back to BUILDING carried the finished
    argument's rounds into the new one and parked on its first REVISE.

    Real sqlite over the real ledger schema, because the SQL is the subject.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ArtifactStore(pathlib.Path(self.dir.name) / "ledger.sqlite3")
        self.addCleanup(self.store.close)
        self.run_id = "run-window"

    def _seed_run(self, plan_revision: int) -> None:
        for revision in range(1, plan_revision + 1):
            self.store.conn.execute(
                "INSERT INTO plan_revisions (run_id, plan_revision, plan_digest, "
                "parent_revision, plan_artifact_ref, amendment_artifact_id, "
                "created_at) VALUES (?,?,?,NULL,?,NULL,'t')",
                (self.run_id, revision, "digest-{0}".format(revision),
                 "plan:{0}".format(revision)),
            )
        columns = [
            "run_id", "runtime_state_root", "runtime_state_fingerprint",
            "plan_digest", "plan_revision", "integration_ref",
            "integration_initial_sha", "target_repository_root",
            "target_git_common_dir", "target_worktree_git_dir",
            "target_object_format", "target_repository_fingerprint",
            "target_sync_journal_fingerprint", "target_initial_main_sha",
            "target_main_ref", "created_at", "updated_at",
        ]
        values = {name: "x" for name in columns}
        values["run_id"] = self.run_id
        values["plan_revision"] = plan_revision
        self.store.conn.execute(
            "INSERT INTO runs ({0}) VALUES ({1})".format(
                ", ".join(columns), ", ".join("?" for _ in columns)
            ),
            [values[name] for name in columns],
        )

    def _round(
        self,
        sequence: int,
        plan_revision: int,
        kind: st.ArtifactKind,
        payload: dict,
    ) -> None:
        self.store.conn.execute(
            "INSERT INTO lane_artifacts (artifact_id, run_id, lane_id, sequence, "
            "completed_stage, artifact_kind, plan_revision, spec_digest, "
            "lane_projection_digest, input_digest, output_digest, artifact_ref, "
            "payload_json, created_at) VALUES (?,?,?,?,'x',?,?,'x','x',?,'x','x',?,'t')",
            (
                "a{0}".format(sequence),
                self.run_id,
                "lane-build",
                sequence,
                kind.value,
                plan_revision,
                "in-{0}".format(sequence),
                json.dumps(payload),
            ),
        )

    def _review(self, sequence: int, plan_revision: int, errors: int) -> None:
        self._round(
            sequence,
            plan_revision,
            st.ArtifactKind.CODE_REVIEW,
            {"public_result_summary": {"failed": errors, "errored": 0}},
        )

    def test_a_superseded_revisions_rounds_are_not_this_arguments_rounds(
        self,
    ) -> None:
        # FDAdb run 2489c772, lane-wp4-clearances-build: three green rounds
        # under revisions 1-3 (the last of which merged), then an amendment,
        # then one REVISE under revision 4. Counting all four made
        # `_stalled` true on the lane's first post-amendment round, and the
        # run reported `waiting` with no window ever granted.
        self._seed_run(4)
        for sequence, revision in ((3, 1), (6, 2), (8, 3), (11, 4)):
            self._review(sequence, revision, 0)
        history = _sealed_error_history(self.store, self.run_id, "lane-build")
        self.assertEqual(history, [0])
        self.assertFalse(_stalled(history))

    def test_rounds_of_the_current_revision_still_stop_the_lane(self) -> None:
        self._seed_run(2)
        self._review(1, 1, 0)
        for sequence in (2, 3, 4):
            self._review(sequence, 2, 5)
        history = _sealed_error_history(self.store, self.run_id, "lane-build")
        self.assertEqual(history, [5, 5, 5])
        self.assertTrue(_stalled(history))

    def test_user_wait_still_resets_inside_one_revision(self) -> None:
        self._seed_run(1)
        for sequence in (1, 2, 3):
            self._review(sequence, 1, 5)
        self._round(4, 1, st.ArtifactKind.USER_WAIT, {})
        self._review(5, 1, 5)
        history = _sealed_error_history(self.store, self.run_id, "lane-build")
        self.assertEqual(history, [5])
        self.assertFalse(_stalled(history))

    def test_the_tests_lane_window_is_scoped_the_same_way(self) -> None:
        self._seed_run(2)
        for sequence, revision in ((1, 1), (2, 1), (3, 1)):
            self._round(
                sequence,
                revision,
                st.ArtifactKind.TEST_REVIEW,
                {"findings": [{"id": "f"}]},
            )
        self._round(
            4, 2, st.ArtifactKind.TEST_REVIEW, {"findings": [{"id": "f"}]}
        )
        history = _test_review_error_history(self.store, self.run_id, "lane-build")
        self.assertEqual(history, [1])
        self.assertFalse(_stalled(history))
