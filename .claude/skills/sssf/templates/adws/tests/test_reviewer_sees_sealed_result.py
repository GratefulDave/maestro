"""The code reviewer is told the sealed result before it votes.

The reviewer agent cannot see the sealed tests and never will: an agent that
could execute them could read them, and the builder's reviewer would then be
judging against tests it had seen. But it was also not told the *outcome*, so
on a red candidate it read a clean-looking diff, returned PASS with no
findings, and the harness downgraded that to REVISE and substituted a canned
sentence -- "sealed private tests failed, errored, or did not execute". The
builder learned that something was broken and nothing about what. A whole
revise round bought it no information.

Observed on FDAdb run f50638ab, lane-wp7-gateway-build: 12 executed, 7 passed,
5 failed, and the only thing the builder was handed was that sentence.

These cases pin the repair:

  * the suite is measured BEFORE the reviewer votes, and exactly once;
  * the reviewer is handed the five public counts -- the same integers that
    already ship to the builder as `public_result_summary`, so nothing private
    moves;
  * a reviewer that still returns no actionable finding against a red suite is
    asked a second time, told that its previous answer was unusable;
  * the canned sentence survives only as the last resort after that second ask.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def _digest(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode()).hexdigest()


def _measurement(**overrides):
    base = dict(
        summary={
            "errored": 0,
            "executed": 12,
            "failed": 5,
            "passed": 7,
            "skipped": 0,
        },
        runner_failed=True,
        collection_broken=False,
        min_cases=1,
        run={},
        files={},
        vault=Path("/state/vault"),
    )
    base.update(overrides)
    return sch.cr.SealedMeasurement(**base)


class _RecordingActor:
    """Records every ctx it is asked with, and replies from a script."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def review_code(self, ctx):
        self.seen.append(ctx)
        return self.replies[min(len(self.seen) - 1, len(self.replies) - 1)]


class _EmptyStore:
    """A store with no recorded rounds, so the stall guard never fires here.

    These cases are about the reviewer's second ask; the no-progress block has
    its own suite. Handing over a real connection that returns nothing keeps
    the production path unguarded rather than special-cased for tests.
    """

    class _Conn:
        def execute(self, *_args):
            return iter(())

    def __init__(self):
        self.conn = self._Conn()


def _scheduler(actor):
    scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
    scheduler.run_id = "run1"
    scheduler.store = _EmptyStore()
    scheduler._provision_argv = ()
    scheduler._provision_timeout_s = 1800.0
    scheduler.runtime = SimpleNamespace(path=Path("/state"))
    scheduler.target = SimpleNamespace(target_repository_root="/repo")
    scheduler.actor = actor
    lane = SimpleNamespace(
        lane_id="lane-a",
        spec_digest=_digest("spec"),
        lane_projection_digest=_digest("projection"),
        public_acceptance=("negative amounts are refused",),
        declared_outputs=("services/api/app.py",),
        lane_kind=st.LANE_KIND_BUILD,
        needs=(),
    )
    row = {"plan_revision": 1, "plan_digest": _digest("plan")}
    artifact = SimpleNamespace(
        artifact_id="art-1",
        payload={
            "builder_base_sha": "1" * 40,
            "candidate_ref": st.candidate_ref("run1", "lane-a", _digest("b")),
            "candidate_sha": "2" * 40,
            "sealed_digest": "3" * 64,
        },
    )
    scheduler._common = lambda lane_id: (row, lane)
    scheduler._sealed_for = lambda lane_arg: artifact
    scheduler._plan_artifact_ref = lambda row_arg: "plan:ref"
    scheduler._sealed_suite_gate = lambda lane_arg: None
    return scheduler, artifact


def _drive(scheduler, artifact, measurement):
    with mock.patch.object(sch, "_latest", return_value=artifact), mock.patch.object(
        sch, "_record_as_lane_artifact", return_value=None
    ), mock.patch.object(
        sch, "_with_input_artifact_ids", side_effect=lambda art, ids: art
    ), mock.patch.object(
        sch, "_complete"
    ), mock.patch.object(
        sch.cr, "measure_candidate", return_value=measurement
    ) as measure, mock.patch.object(
        sch.cr, "review_builder_output", return_value=None
    ) as review:
        scheduler._reviewing_code("lane-a")
    return measure, review


class ReviewerSeesTheSealedResult(unittest.TestCase):
    def test_the_reviewer_is_asked_after_the_suite_is_measured(self):
        actor = _RecordingActor([(st.ReviewerVerdict.PASS, ())])
        scheduler, artifact = _scheduler(actor)
        measurement = _measurement(runner_failed=False, summary={
            "errored": 0, "executed": 12, "failed": 0, "passed": 12, "skipped": 0
        })

        _drive(scheduler, artifact, measurement)

        self.assertEqual(len(actor.seen), 1)
        self.assertEqual(
            dict(actor.seen[0].sealed_result_summary),
            {"errored": 0, "executed": 12, "failed": 0, "passed": 12, "skipped": 0},
        )

    def test_the_suite_runs_once_not_once_per_actor(self):
        # Provisioning a review tree costs minutes. Measuring in the scheduler
        # and again inside review_builder_output would double every review.
        actor = _RecordingActor([(st.ReviewerVerdict.REVISE, ({"a": "b"},))])
        scheduler, artifact = _scheduler(actor)
        measurement = _measurement()

        measure, review = _drive(scheduler, artifact, measurement)

        self.assertEqual(measure.call_count, 1)
        self.assertIs(review.call_args.kwargs["measurement"], measurement)

    def test_a_red_suite_with_no_findings_is_asked_a_second_time(self):
        located = {
            "implementation_area": "services/api/app.py",
            "observed_behavior": "negative amounts are accepted",
            "required_behavior": "reject amounts below zero",
            "violated_requirement": "negative amounts are refused",
        }
        actor = _RecordingActor(
            [(st.ReviewerVerdict.PASS, ()), (st.ReviewerVerdict.REVISE, (located,))]
        )
        scheduler, artifact = _scheduler(actor)

        _, review = _drive(scheduler, artifact, _measurement())

        self.assertEqual(len(actor.seen), 2)
        self.assertFalse(actor.seen[0].sealed_findings_required)
        self.assertTrue(actor.seen[1].sealed_findings_required)
        self.assertEqual(review.call_args.kwargs["findings"], (located,))

    def test_a_reviewer_that_already_located_the_defect_is_not_re_asked(self):
        located = {
            "implementation_area": "services/api/app.py",
            "observed_behavior": "the missing-config path bypasses every request",
            "required_behavior": "bypass only injected fetchImpl calls",
            "violated_requirement": "negative amounts are refused",
        }
        actor = _RecordingActor([(st.ReviewerVerdict.REVISE, (located,))])
        scheduler, artifact = _scheduler(actor)

        _drive(scheduler, artifact, _measurement())

        self.assertEqual(len(actor.seen), 1)

    def test_a_green_suite_never_triggers_a_second_ask(self):
        actor = _RecordingActor([(st.ReviewerVerdict.PASS, ())])
        scheduler, artifact = _scheduler(actor)
        measurement = _measurement(runner_failed=False)

        _drive(scheduler, artifact, measurement)

        self.assertEqual(len(actor.seen), 1)


class TheReviewerPromptCarriesTheCounts(unittest.TestCase):
    """The counts have to reach the agent's prompt, not just its ctx."""

    @staticmethod
    def _instructions(summary, *, required=False):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        ctx = SimpleNamespace(
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=st.LANE_KIND_BUILD),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.REVIEWING_CODE,
        )
        extra = {"sealed_result_summary": summary}
        if required:
            extra["sealed_findings_required"] = True
        body = maestro.HerdrStageActor._prompt(
            actor,
            ctx,
            "code-reviewer",
            Path("/tmp/envelope.json"),
            Path("/tmp/cwd"),
            extra,
        )
        return body["instructions"]

    def test_a_red_summary_tells_the_reviewer_to_locate_the_defect(self):
        text = self._instructions(
            {"errored": 0, "executed": 12, "failed": 5, "passed": 7, "skipped": 0}
        )
        self.assertIn("12 executed", text)
        self.assertIn("5 failed", text)
        self.assertIn("REVISE", text)
        self.assertIn("declared_outputs", text)
        self.assertIn("Do not restate that tests failed", text)

    def test_a_green_summary_adds_no_red_instruction(self):
        text = self._instructions(
            {"errored": 0, "executed": 12, "failed": 0, "passed": 12, "skipped": 0}
        )
        self.assertNotIn("The suite is red", text)

    def test_zero_executed_reads_as_red(self):
        text = self._instructions(
            {"errored": 0, "executed": 0, "failed": 0, "passed": 0, "skipped": 0}
        )
        self.assertIn("The suite is red", text)

    def test_the_second_ask_says_the_first_answer_was_unusable(self):
        text = self._instructions(
            {"errored": 0, "executed": 12, "failed": 5, "passed": 7, "skipped": 0},
            required=True,
        )
        self.assertIn("carried no actionable finding", text)

    def test_the_counts_reach_the_prompt_body(self):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.lane_specs = {}
        summary = {
            "errored": 0,
            "executed": 12,
            "failed": 5,
            "passed": 7,
            "skipped": 0,
        }
        ctx = SimpleNamespace(
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=st.LANE_KIND_BUILD),
            plan_revision=1,
            run_id="run1",
            stage=st.LaneStage.REVIEWING_CODE,
        )
        body = maestro.HerdrStageActor._prompt(
            actor,
            ctx,
            "code-reviewer",
            Path("/tmp/envelope.json"),
            Path("/tmp/cwd"),
            {"sealed_result_summary": summary},
        )
        self.assertEqual(body["sealed_result_summary"], summary)


if __name__ == "__main__":
    unittest.main()


class StepsAreReported(unittest.TestCase):
    """A stage that runs for minutes reports what it is doing while it runs.

    Before this, REVIEWING_CODE printed a start line, then nothing for two to
    three minutes while it provisioned a tree and ran the suite, then a
    completion line. An operator watching a lane could not distinguish
    provisioning from a hung agent from a dead scheduler -- and reported the
    lane as "doing nothing" when it was mid-provision.
    """

    def _steps(self, measurement, replies):
        actor = _RecordingActor(replies)
        scheduler, artifact = _scheduler(actor)
        said = []
        scheduler.step = lambda lane, msg, detail="": said.append((lane, msg, detail))
        _drive(scheduler, artifact, measurement)
        return [msg for _lane, msg, _detail in said]

    def test_provisioning_and_the_suite_are_announced_before_the_reviewer(self):
        steps = self._steps(
            _measurement(), [(st.ReviewerVerdict.REVISE, ({"a": "b"},))]
        )
        self.assertIn("provisioning review tree and running sealed suite", steps)
        self.assertIn("sealed suite FAILED", steps)
        self.assertIn("asking code reviewer", steps)
        self.assertLess(
            steps.index("provisioning review tree and running sealed suite"),
            steps.index("asking code reviewer"),
        )

    def test_the_counts_are_carried_as_the_detail(self):
        actor = _RecordingActor([(st.ReviewerVerdict.REVISE, ({"a": "b"},))])
        scheduler, artifact = _scheduler(actor)
        said = []
        scheduler.step = lambda lane, msg, detail="": said.append((lane, msg, detail))
        _drive(scheduler, artifact, _measurement())
        detail = next(d for _l, m, d in said if m == "sealed suite FAILED")
        self.assertEqual(detail, "12 executed, 7 passed, 5 failed, 0 errored")

    def test_a_green_suite_says_passed(self):
        steps = self._steps(
            _measurement(
                runner_failed=False,
                summary={
                    "errored": 0,
                    "executed": 12,
                    "failed": 0,
                    "passed": 12,
                    "skipped": 0,
                },
            ),
            [(st.ReviewerVerdict.PASS, ())],
        )
        self.assertIn("sealed suite passed", steps)

    def test_the_second_ask_is_announced(self):
        steps = self._steps(
            _measurement(),
            [(st.ReviewerVerdict.PASS, ()), (st.ReviewerVerdict.REVISE, ({"a": "b"},))],
        )
        self.assertIn(
            "no actionable finding against a red suite, asking again", steps
        )
        self.assertIn("code reviewer answered REVISE on the second ask", steps)

    def test_a_reporter_that_raises_cannot_fail_the_lane(self):
        # Reporting is never workflow state. A broken console must not be able
        # to take a lane down with it.
        actor = _RecordingActor([(st.ReviewerVerdict.REVISE, ({"a": "b"},))])
        scheduler, artifact = _scheduler(actor)

        def explode(*_args, **_kwargs):
            raise RuntimeError("console is on fire")

        scheduler.step = explode
        _drive(scheduler, artifact, _measurement())  # must not raise

    def test_a_scheduler_with_no_reporter_is_silent_and_fine(self):
        actor = _RecordingActor([(st.ReviewerVerdict.REVISE, ({"a": "b"},))])
        scheduler, artifact = _scheduler(actor)
        _drive(scheduler, artifact, _measurement())  # no .step attribute at all
