"""Past the suite-reopen cap, findings only about the suite park the build lane.

The builder may not edit the accepted suite, so a REVISE whose every finding is
attributed to it can only waste builder rounds. When the reopen cap is spent
such a lane parks NO_PROGRESS for attend at once; a review with any candidate
finding still goes to the builder. A PAUSEd lane in that state parks the same
way on resume instead of returning to BUILDING.
"""

from __future__ import annotations

import unittest

from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from tests.test_suite_reopen import ReopenActor, SUITE_PATH, SuiteReopenTests
from tests.test_tests_lane_handoff import _finding_for


class MixedActor(ReopenActor):
    """'mixed' answers one suite finding and one candidate finding."""

    def review_code(self, ctx):
        answer = self.reviews[0]
        if answer != "mixed":
            return super().review_code(ctx)
        self.calls.append("review_code")
        self.review_code_lanes.append(ctx.lane.lane_id)
        suite = dict(
            _finding_for(ctx),
            attribution=st.FINDING_ATTRIBUTION_SUITE,
            implementation_area=SUITE_PATH + ":4 the fixture reads the wrong file",
        )
        return st.ReviewerVerdict.REVISE, (suite, _finding_for(ctx))


class SuiteCapPark(unittest.TestCase):
    setUp = SuiteReopenTests.setUp
    _start = SuiteReopenTests._start
    _compiled = SuiteReopenTests._compiled

    def _run(self, answer: str, name: str):
        actor = MixedActor(
            self.repo, self.runtime.path / "worktrees", reviews=[answer]
        )
        scheduler = self._start(self._compiled(), actor, name)
        scheduler.run()
        return actor, scheduler

    def _wait(self, name: str):
        return sch._latest(
            self.store, name, "lane-build", st.ArtifactKind.USER_WAIT
        )

    def test_only_suite_findings_park_without_another_builder_round(self) -> None:
        actor, _ = self._run("suite", "run-cap-suite")
        # The cap is spent after MAX_SUITE_REOPENS redrafts; the review that
        # finds it spent parks the lane, so no builder round follows it.
        self.assertEqual(actor.calls[-1], "review_code")
        self.assertEqual(actor.calls.count("build"), 1 + st.MAX_SUITE_REOPENS)
        self.assertEqual(
            self.store.lane_stage("run-cap-suite", "lane-build"),
            st.LaneStage.WAITING_FOR_USER,
        )
        self.assertEqual(
            self._wait("run-cap-suite").payload["wait_reason"],
            st.WaitReason.NO_PROGRESS.value,
        )

    def test_mixed_findings_still_go_to_the_builder(self) -> None:
        actor, _ = self._run("mixed", "run-cap-mixed")
        self.assertGreater(actor.calls.count("build"), 1 + st.MAX_SUITE_REOPENS)

    def test_resume_of_a_paused_lane_parks_instead_of_building(self) -> None:
        actor, scheduler = self._run("suite", "run-cap-resume")
        builds = actor.calls.count("build")
        # Reproduce the live state: the lane was PAUSEd (Ctrl-C) at BUILDING
        # after the cap-reached REVISE, rather than parked NO_PROGRESS.
        self.store.resume_lane("run-cap-resume", "lane-build")
        digest, observed = scheduler._pause_input("lane-build", st.LaneStage.BUILDING)
        self.store.pause_lane(
            "run-cap-resume", "lane-build", st.LaneStage.BUILDING, digest,
            observed=observed, reason=st.WaitReason.PAUSE,
        )
        self.assertEqual(
            self._wait("run-cap-resume").payload["wait_reason"], "PAUSE"
        )
        scheduler.resume_waiting()
        self.assertEqual(
            self.store.lane_stage("run-cap-resume", "lane-build"),
            st.LaneStage.WAITING_FOR_USER,
        )
        self.assertEqual(
            self._wait("run-cap-resume").payload["wait_reason"],
            st.WaitReason.NO_PROGRESS.value,
        )
        self.assertEqual(actor.calls.count("build"), builds)


if __name__ == "__main__":
    unittest.main()
