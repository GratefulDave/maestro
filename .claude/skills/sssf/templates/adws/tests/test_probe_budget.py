"""The probe budget bounds probe-introducing rounds per window, not the verdict.

A probe round is a code review since the latest USER_WAIT that admitted a
probe. Exhausting the budget parks the lane at BUILDING through the existing
NO_PROGRESS pause, after the review that spent it has completed as an ordinary
REVISE. A failing admitted probe promotes a PASS whatever the budget says.
"""

import json
import unittest
from unittest import mock

from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from tests.probe_lane_support import ProbeLaneCase, probe_path
from tests.test_tests_lane_handoff import _git, _unified_plan_bytes

REVISE = st.ReviewerVerdict.REVISE
PASS = st.ReviewerVerdict.PASS


class ExhaustionTest(ProbeLaneCase):
    def test_exhaustion_parks_at_building_after_a_complete_revise(self):
        self.start(
            ["8", "8 alpha"],
            [{"verdict": REVISE, "probes": ["alpha"]}, {"verdict": REVISE, "probes": ["beta"]}],
            max_probe_rounds=2,
        )
        self.round()
        self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)
        wait = self.waits()[0]
        self.assertEqual(wait["wait_reason"], st.WaitReason.NO_PROGRESS.value)
        self.assertEqual(wait["resume_stage"], st.LaneStage.BUILDING.value)
        last = self.reviews()[-1]
        self.assertEqual(last["verdict"], "REVISE")
        self.assertEqual(len(last["probe_records"]), 2, "the review that spent the budget carries its probe")

    def test_a_pass_with_a_failing_probe_at_exhaustion_is_a_revise(self):
        self.start(["8"], [{"verdict": REVISE, "probes": ["alpha"]}], max_probe_rounds=1)
        with mock.patch.object(self.scheduler, "_park_no_progress"):
            self.round()  # the pause never happens: a crash between completion and pause
        self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.assertEqual(self.scheduler._probe_rounds_used(self.lane_id), 1)
        # The next build is dispatched as if the budget had not been checked.
        self.scheduler.max_probe_rounds = 5
        self.scheduler._building(self.lane_id)
        self.scheduler.max_probe_rounds = 1
        self.actor.script = [{"verdict": PASS, "probes": ["beta"]}]
        self.actor.review_calls = 0
        self.scheduler._reviewing_code(self.lane_id)
        review = self.reviews()[-1]
        self.assertEqual(review["verdict"], "REVISE", "alpha is still open: a PASS cannot stand")
        outcomes = {s["path"]: s["outcome"] for s in review["probe_submissions"]}
        self.assertEqual(outcomes, {probe_path("beta"): "IGNORED_BUDGET"})
        self.assertEqual(self.actor.accept_calls[1:], [], "an ignored submission is not measured or accepted")
        self.assertEqual(list(review["probe_status"].values()), ["OPEN"])
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)

    def test_a_crash_between_completion_and_pause_parks_at_building_entry(self):
        self.start(["8", "8 alpha"], [{"verdict": REVISE, "probes": ["alpha"]}], max_probe_rounds=1)
        with mock.patch.object(self.scheduler, "_park_no_progress"):
            self.round()
        self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.assertEqual(self.waits(), [])
        self.scheduler._building(self.lane_id)
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual(self.actor.builds, 1, "no builder was dispatched past the budget")
        self.assertEqual(self.waits()[0]["wait_reason"], st.WaitReason.NO_PROGRESS.value)

    def test_resume_keeps_the_probes_resets_the_count_and_merges_nothing(self):
        self.start(["8", "8 alpha"], [{"verdict": REVISE, "probes": ["alpha"]}, {"verdict": REVISE}], max_probe_rounds=1)
        self.round()
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)
        before = _git(self.repo, "rev-parse", st.integration_ref(self.run_id))
        self.scheduler.resume_waiting()
        self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.assertEqual(self.scheduler._probe_rounds_used(self.lane_id), 0)
        records, retired, _, commit = self.scheduler._accumulated_probes(self.lane_id)
        self.assertEqual([r["path"] for r in records], [probe_path("alpha")])
        self.assertEqual(self.scheduler._probe_budget_exhausted(self.lane_id), False)
        self.round()
        self.assertEqual(self.actor.builder_ctxs[-1].probe_texts[probe_path("alpha")][:4], "from")
        self.assertEqual(_git(self.repo, "rev-parse", st.integration_ref(self.run_id)), before)


class DisabledAndRevisionTest(ProbeLaneCase):
    def test_zero_turns_new_submissions_off(self):
        self.start(["8"], [{"verdict": REVISE, "probes": ["alpha"]}], max_probe_rounds=0)
        self.round()
        review = self.reviews()[0]
        self.assertNotIn("probe_records", review)
        self.assertNotIn("probe_submissions", review)
        self.assertEqual(self.actor.accept_calls, [])
        self.assertEqual(self.actor.review_ctxs[0].probe_budget_left, 0)

    def test_a_plan_revision_drops_the_accumulated_probes(self):
        self.start(["8"], [{"verdict": REVISE, "probes": ["alpha"]}])
        self.round()
        self.assertEqual(len(self.scheduler._accumulated_probes(self.lane_id)[0]), 1)
        amended = plan_compiler.compile_plan(
            _unified_plan_bytes(goal="emit a.txt with detail"),
            plan_revision=2,
            plan_artifact_ref="plan:probes-2",
        )
        sch.apply_factory_amendment(
            self.store, self.run_id, amended, runtime=self.runtime, target=self.target
        )
        self.assertEqual(self.scheduler._accumulated_probes(self.lane_id), ((), (), (), ""))
        self.assertEqual(self.scheduler._probe_rounds_used(self.lane_id), 0)


class ConstructorTest(unittest.TestCase):
    def test_the_kwarg_and_the_class_default_agree(self):
        self.assertEqual(sch.FactoryScheduler.max_probe_rounds, 5)
        for bad in (-1, True):
            with self.assertRaises(ValueError):
                sch.FactoryScheduler.__init__(object.__new__(sch.FactoryScheduler), None, "r", None, None, None, max_probe_rounds=bad)


if __name__ == "__main__":
    unittest.main()
