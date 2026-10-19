"""A broken accepted suite goes back to its tests lane, not to the builder.

A build lane's builder may not edit its accepted suite
(`CANDIDATE_TEST_PATH_REFUSED`), so a fixture or harness defect in that suite
used to cost every builder round until NO_PROGRESS, and only a plan amendment
could answer it. Two typed triggers now send the suite back to its paired tests
lane with the evidence: a code-review finding attributed to the accepted suite
and citing a suite path and line, or the suite crashing the same way on two
different candidates (maestro#330). The build lane waits at BUILDING until the
re-sealed suite merges, then rebuilds against it. The tests lane's contract is
unchanged and its test reviewer still has to PASS the repair. A tests lane is
reopened at most `MAX_SUITE_REOPENS` times per revision; past that the build
lane takes the ordinary REVISE path to NO_PROGRESS.

`resume_waiting(hold_no_progress=True)` is what `run attend` (and `run resume`
with attend enabled) calls: a NO_PROGRESS park recorded under the current
revision is not regranted rounds (maestro#331); it goes to the suite reopen when
the recorded reviews attribute the defect to the suite, and otherwise stays
parked for attend.

Real git, real pytest, real ledger; only the agents are fakes.
"""

from __future__ import annotations

import json
import signal
import unittest
from collections import defaultdict
from unittest import mock

from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules import tests_chain as tc
from tests import test_tests_lane_handoff as handoff
from tests.test_tests_lane_handoff import (
    HandoffActor,
    _finding_for,
    _plan_bytes,
)

SUITE_PATH = "tests/public_contract.py"

_ASSERTS = (
    "from pathlib import Path\n"
    "def test_product_is_ready():\n"
    "    assert Path('product.py').is_file()\n"
    "    assert Path('product.py').read_text().strip().endswith('ready')\n"
)
_CRASHES = (
    "import os, signal\n"
    "def test_product_is_ready():\n"
    "    os.kill(os.getpid(), signal.SIGKILL)\n"
)


class ReopenActor(HandoffActor):
    """Scripted reviewer answers; the tester writes one suite version per turn."""

    def __init__(self, repo, worktrees, *, reviews, first_suite=_ASSERTS) -> None:
        super().__init__(repo, worktrees)
        self.reviews = list(reviews)
        self.first_suite = first_suite
        self.tests_turns: list[dict] = []
        self.calls: list[str] = []
        self.same_candidate = False
        self.versions: dict[str, int] = defaultdict(int)

    def write_tests(self, ctx):
        self.calls.append("write_tests")
        review = ctx.artifacts.get("TEST_REVIEW")
        self.tests_turns.append(
            {
                "contract": json.dumps(ctx.public_contract, sort_keys=True),
                "review": dict(review.payload) if review is not None else None,
            }
        )
        self.write_tests_lanes.append(ctx.lane.lane_id)
        version = len(self.tests_turns)
        body = self.first_suite if version == 1 else _ASSERTS
        return {
            "files": {
                path: "# version {0}\n{1}".format(version, body)
                for path in ctx.lane.declared_outputs
            }
        }

    def review_tests(self, ctx):
        self.calls.append("review_tests")
        return super().review_tests(ctx)

    def build(self, ctx):
        self.calls.append("build")
        return super().build(ctx)

    def review_code(self, ctx):
        self.calls.append("review_code")
        self.review_code_lanes.append(ctx.lane.lane_id)
        answer = self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]
        if answer == "pass":
            return st.ReviewerVerdict.PASS, ()
        finding = _finding_for(ctx)
        if answer == "suite":
            finding = dict(
                finding,
                attribution=st.FINDING_ATTRIBUTION_SUITE,
                implementation_area=SUITE_PATH + ":4 the fixture reads the wrong file",
            )
        return st.ReviewerVerdict.REVISE, (finding,)


def _reopens(store, run_id: str) -> list[dict]:
    return [
        json.loads(row[0])["suite_reopen"]
        for row in store.conn.execute(
            "SELECT payload_json FROM lane_artifacts WHERE run_id=? AND lane_id=? "
            "AND artifact_kind=? ORDER BY sequence",
            (run_id, "lane-tests", st.ArtifactKind.TEST_REVIEW.value),
        )
        if "suite_reopen" in json.loads(row[0])
    ]


def _code_reviews(store, run_id: str) -> list[dict]:
    return [
        json.loads(row[0])
        for row in store.conn.execute(
            "SELECT payload_json FROM lane_artifacts WHERE run_id=? AND lane_id=? "
            "AND artifact_kind=? ORDER BY sequence",
            (run_id, "lane-build", st.ArtifactKind.CODE_REVIEW.value),
        )
    ]


class SuiteCrashReason(unittest.TestCase):
    def test_signal_exit_codes_are_typed(self) -> None:
        self.assertEqual(
            tc.suite_crash_reason(128 + int(signal.SIGBUS)), "SUITE_CRASHED:SIGBUS"
        )
        self.assertEqual(
            tc.suite_crash_reason(-int(signal.SIGKILL)), "SUITE_CRASHED:SIGKILL"
        )

    def test_ordinary_exits_are_not_crashes(self) -> None:
        for code in (0, 1, 2, 4, 5, -1):
            self.assertEqual(tc.suite_crash_reason(code), "")


class SuiteReopenTests(unittest.TestCase):
    """The handoff fixture's repository, runtime and `_start`, not its tests."""

    setUp = handoff.TestsLaneHandoffTests.setUp
    _start = handoff.TestsLaneHandoffTests._start

    def _compiled(self, revision: int = 1, **goals):
        return plan_compiler.compile_plan(
            _plan_bytes(**goals),
            plan_revision=revision,
            plan_artifact_ref="plan:reopen-{0}".format(revision),
        )

    def test_attributed_suite_defect_reopens_tests_lane_and_holds_build(self) -> None:
        actor = ReopenActor(
            self.repo, self.runtime.path / "worktrees", reviews=["suite", "pass"]
        )
        scheduler = self._start(self._compiled(), actor, "run-reopen")
        status = scheduler.run()

        self.assertEqual(status, st.RunStatus.COMPLETE)
        # The build lane was not dispatched again until the tests lane had
        # redrafted, been reviewed and merged.
        self.assertEqual(
            actor.calls,
            [
                "write_tests",
                "review_tests",
                "build",
                "review_code",
                "write_tests",
                "review_tests",
                "build",
                "review_code",
            ],
        )
        reopens = _reopens(self.store, "run-reopen")
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["trigger"], "REVIEWER_ATTRIBUTED")
        self.assertEqual(reopens[0]["build_lane_id"], "lane-build")
        # The tester was handed the reviewer's located finding as a REVISE.
        second = actor.tests_turns[1]["review"]
        self.assertEqual(second["verdict"], "REVISE")
        self.assertTrue(second["findings"][0]["implementation_area"].startswith(SUITE_PATH + ":4"))
        # Same contract on both turns: only the suite implementation moved.
        self.assertEqual(actor.tests_turns[0]["contract"], actor.tests_turns[1]["contract"])
        self.assertEqual(
            self.store.conn.execute(
                "SELECT plan_revision FROM runs WHERE run_id=?", ("run-reopen",)
            ).fetchone()[0],
            1,
        )
        # The rebuild ran against the re-sealed suite.
        reviews = _code_reviews(self.store, "run-reopen")
        self.assertNotEqual(reviews[0]["test_suite_digest"], reviews[1]["test_suite_digest"])
        self.assertEqual(
            actor.building_entries,
            [
                ("lane-build", st.BuildingEntryKind.INITIAL),
                ("lane-build", st.BuildingEntryKind.CODE_REVISE),
            ],
        )
        self.assertIn(
            ("MERGED", "WRITING_TESTS", "suite_reopen"),
            {
                tuple(row)
                for row in self.store.conn.execute(
                    "SELECT from_stage, to_stage, reason FROM transitions "
                    "WHERE run_id=? AND lane_id=?",
                    ("run-reopen", "lane-tests"),
                )
            },
        )

    def test_unattributed_or_uncited_finding_does_not_reopen(self) -> None:
        self.assertFalse(sch._cites_suite_line("tests/public_contract.py", (SUITE_PATH,)))
        self.assertFalse(sch._cites_suite_line("product.py:3", (SUITE_PATH,)))
        self.assertTrue(sch._cites_suite_line("see tests/public_contract.py:12", (SUITE_PATH,)))
        actor = ReopenActor(
            self.repo, self.runtime.path / "worktrees", reviews=["candidate", "pass"]
        )
        scheduler = self._start(self._compiled(), actor, "run-plain")
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        self.assertEqual(_reopens(self.store, "run-plain"), [])
        self.assertEqual(actor.calls.count("write_tests"), 1)

    def test_reopen_cap_falls_back_to_no_progress(self) -> None:
        actor = ReopenActor(
            self.repo, self.runtime.path / "worktrees", reviews=["suite"]
        )
        scheduler = self._start(self._compiled(), actor, "run-cap")
        status = scheduler.run()

        self.assertEqual(status, st.RunStatus.WAITING)
        self.assertEqual(len(_reopens(self.store, "run-cap")), st.MAX_SUITE_REOPENS)
        self.assertEqual(actor.calls.count("write_tests"), 1 + st.MAX_SUITE_REOPENS)
        self.assertEqual(
            self.store.lane_stage("run-cap", "lane-build"),
            st.LaneStage.WAITING_FOR_USER,
        )
        self.assertEqual(
            self.store.lane_stage("run-cap", "lane-tests"), st.LaneStage.MERGED
        )
        wait = sch._latest(
            self.store, "run-cap", "lane-build", st.ArtifactKind.USER_WAIT
        )
        self.assertEqual(wait.payload["wait_reason"], st.WaitReason.NO_PROGRESS.value)

    def test_suite_crash_on_two_candidates_reopens(self) -> None:
        actor = ReopenActor(
            self.repo,
            self.runtime.path / "worktrees",
            reviews=["candidate", "candidate", "pass"],
            first_suite=_CRASHES,
        )
        scheduler = self._start(self._compiled(), actor, "run-crash")
        status = scheduler.run()

        self.assertEqual(status, st.RunStatus.COMPLETE)
        reviews = _code_reviews(self.store, "run-crash")
        for crashed in reviews[:2]:
            summary = crashed["public_result_summary"]
            self.assertEqual(summary["crashed"], "SUITE_CRASHED:SIGKILL")
            self.assertEqual(crashed["failure_output"][0], "SUITE_CRASHED:SIGKILL")
        self.assertNotEqual(reviews[0]["candidate_sha"], reviews[1]["candidate_sha"])
        reopens = _reopens(self.store, "run-crash")
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["trigger"], "SUITE_CRASHED")
        handed = actor.tests_turns[1]["review"]["findings"][0]
        self.assertIn("SUITE_CRASHED:SIGKILL", handed["observed_behavior"])
        self.assertEqual(handed["attribution"], st.FINDING_ATTRIBUTION_SUITE)
        # One crash alone did not reopen: the first REVISE went to the builder.
        self.assertEqual(
            actor.calls[:6],
            ["write_tests", "review_tests", "build", "review_code", "build", "review_code"],
        )
        self.assertNotIn("crashed", reviews[2]["public_result_summary"])

    # -- maestro#331: no regrant under attend -------------------------------

    def _park_no_progress(self, run_id: str, reviews) -> ReopenActor:
        actor = ReopenActor(self.repo, self.runtime.path / "worktrees", reviews=reviews)
        scheduler = self._start(self._compiled(), actor, run_id)
        self.assertEqual(scheduler.run(), st.RunStatus.WAITING)
        self.assertEqual(
            self.store.lane_stage(run_id, "lane-build"), st.LaneStage.WAITING_FOR_USER
        )
        return actor

    def _scheduler(self, run_id: str, actor, compiled=None):
        return sch.FactoryScheduler(
            self.store,
            run_id,
            actor,
            self.runtime,
            gitpub.bind_target_worktree(self.repo, "refs/heads/main"),
            compiled=compiled or self._compiled(),
        )

    def test_attend_resume_does_not_regrant_no_progress(self) -> None:
        actor = self._park_no_progress("run-hold", ["candidate"])
        self._scheduler("run-hold", actor).resume_waiting(hold_no_progress=True)
        self.assertEqual(
            self.store.lane_stage("run-hold", "lane-build"),
            st.LaneStage.WAITING_FOR_USER,
        )
        self.assertEqual(_reopens(self.store, "run-hold"), [])

    def test_attend_disabled_resume_keeps_regrant(self) -> None:
        actor = self._park_no_progress("run-regrant", ["candidate"])
        self._scheduler("run-regrant", actor).resume_waiting()
        self.assertEqual(
            self.store.lane_stage("run-regrant", "lane-build"), st.LaneStage.BUILDING
        )

    def test_amendment_since_park_keeps_regrant(self) -> None:
        actor = self._park_no_progress("run-amended", ["candidate"])
        amended = self._compiled(revision=2, build_goal="implement product.py again")
        sch.apply_factory_amendment(
            self.store,
            "run-amended",
            amended,
            runtime=self.runtime,
            target=gitpub.bind_target_worktree(self.repo, "refs/heads/main"),
        )
        self._scheduler("run-amended", actor, amended).resume_waiting(
            hold_no_progress=True
        )
        self.assertNotEqual(
            self.store.lane_stage("run-amended", "lane-build"),
            st.LaneStage.WAITING_FOR_USER,
        )

    def test_attend_resume_routes_attributed_park_to_reopen(self) -> None:
        # Parked by a runtime that had no reopen path: the reviews already
        # attribute the defect to the suite.
        with mock.patch.object(
            sch.FactoryScheduler, "_suite_reopen_plan", return_value=None
        ):
            actor = self._park_no_progress("run-route", ["suite"])
        self.assertEqual(_reopens(self.store, "run-route"), [])
        self._scheduler("run-route", actor).resume_waiting(hold_no_progress=True)
        self.assertEqual(
            self.store.lane_stage("run-route", "lane-build"), st.LaneStage.BUILDING
        )
        self.assertEqual(
            self.store.lane_stage("run-route", "lane-tests"),
            st.LaneStage.WRITING_TESTS,
        )
        self.assertEqual(len(_reopens(self.store, "run-route")), 1)
        # The build lane is held: only the tests lane is ready.
        self.assertEqual(self.store.ready_lane_ids("run-route"), ("lane-tests",))


if __name__ == "__main__":
    unittest.main()
