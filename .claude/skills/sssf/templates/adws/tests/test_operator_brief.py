"""A parked lane renders its own typed records as an operator decision.

Every case drives the real `ArtifactStore` and the real read path. The review
rows are synthesized -- a brief is a reader, and building three genuine review
rounds through five agents would test the factory, not the reader -- but they
are inserted into the real schema of a real run and read back through the same
public method the scheduler and the CLI call.
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Sequence
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import operator_brief as ob
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from tests.test_no_progress_block import ContentActor, _unified_plan_bytes
from tests.test_single_entry_cli import (
    ScriptedActor,
    SingleEntryBase,
    ship_plan,
    working_directory,
)
from tests.test_run_status import _init_repo, _plan_bytes


def _digest(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _finding(area: str, requirement: str) -> dict:
    return {
        "implementation_area": area,
        "observed_behavior": "the observed behavior",
        "required_behavior": "the required behavior",
        "violated_requirement": requirement,
    }


def _summary(executed: int, failed: int) -> dict:
    return {
        "executed": executed,
        "passed": executed - failed,
        "failed": failed,
        "errored": 0,
        "skipped": 0,
    }


class BriefFromRealLedgerTest(unittest.TestCase):
    lane_id = "lane-a"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.store.close)
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:brief"
        )
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        self.run_id = "run-brief"
        sch.create_factory_run(
            store=self.store,
            run_id=self.run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )

    # -- row construction ---------------------------------------------------

    def _insert(
        self,
        *,
        kind: st.ArtifactKind,
        sequence: int,
        payload: Mapping[str, Any],
        completed_stage: st.LaneStage,
    ) -> None:
        seed = "{0}:{1}".format(kind.value, sequence)
        self.store.conn.execute(
            "INSERT INTO lane_artifacts ("
            "artifact_id, run_id, lane_id, sequence, completed_stage, "
            "artifact_kind, plan_revision, spec_digest, lane_projection_digest, "
            "input_digest, output_digest, artifact_ref, payload_json, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "artifact-" + seed,
                self.run_id,
                self.lane_id,
                sequence,
                completed_stage.value,
                kind.value,
                1,
                _digest("spec"),
                _digest("projection"),
                _digest("input:" + seed),
                _digest("output:" + seed),
                "ref:" + seed,
                json.dumps(payload, sort_keys=True),
                "2026-09-10T0{0}:00:00Z".format(min(9, sequence % 10)),
            ),
        )
        self.store.conn.commit()

    def _review(
        self,
        sequence: int,
        *,
        findings: Sequence[Mapping[str, Any]],
        failed: int,
        kind: st.ArtifactKind = st.ArtifactKind.CODE_REVIEW,
    ) -> None:
        payload = {
            "candidate_sha": "0" * 39 + str(sequence % 10),
            "findings": [dict(item) for item in findings],
            "public_result_summary": _summary(11, failed),
            "verdict": st.ReviewerVerdict.REVISE.value,
        }
        self._insert(
            kind=kind,
            sequence=sequence,
            payload=payload,
            completed_stage=st.LaneStage.REVIEWING_CODE,
        )

    def _wait(
        self,
        reason: st.WaitReason,
        *,
        sequence: int = 900,
        resume_stage: st.LaneStage = st.LaneStage.BUILDING,
    ) -> None:
        self._insert(
            kind=st.ArtifactKind.USER_WAIT,
            sequence=sequence,
            payload={
                "predecessor_sequence": sequence - 1,
                "resume_stage": resume_stage.value,
                "wait_reason": reason.value,
            },
            completed_stage=st.LaneStage.WAITING_FOR_USER,
        )

    def brief(self) -> str:
        return ob.waiting_brief(self.store, self.run_id, self.lane_id)

    # -- cases --------------------------------------------------------------

    def test_a_lane_that_never_parked_has_no_brief(self) -> None:
        self.assertEqual(self.brief(), "")

    def test_no_progress_with_a_different_finding_each_round_is_converging(self) -> None:
        self._review(101, findings=[_finding("store", "claim-one")], failed=2)
        self._review(103, findings=[_finding("store", "claim-two")], failed=2)
        self._review(105, findings=[_finding("api", "claim-three")], failed=1)
        self._wait(st.WaitReason.NO_PROGRESS)
        text = self.brief()
        self.assertIn("converging", text)
        self.assertNotIn("repeating", text)
        self.assertIn("Reviews 101,103: 2 failed; 105: 1 failed", text)
        self.assertIn("verdict REVISE each round", text)
        self.assertIn('"claim-three"', text)
        self.assertIn('"api"', text)
        self.assertIn(
            "another {0} rounds".format(st.NO_PROGRESS_GRACE_ROUNDS), text
        )
        self.assertIn(
            "uv run adws/maestro.py run resume {0}".format(self.run_id), text
        )
        self.assertNotIn("run amend", text)

    def test_no_progress_with_the_same_finding_each_round_is_repeating(self) -> None:
        finding = _finding("ObservationStore.record", "claim-wp1-provenance")
        for sequence in (101, 103, 105):
            self._review(sequence, findings=[finding], failed=2)
        self._wait(st.WaitReason.NO_PROGRESS)
        text = self.brief()
        self.assertIn("repeating", text)
        self.assertNotIn("converging", text)
        self.assertIn("survived 3 rounds", text)
        self.assertIn('"claim-wp1-provenance"', text)
        self.assertIn('"ObservationStore.record"', text)
        self.assertIn(
            "uv run adws/maestro.py run resume {0}".format(self.run_id), text
        )
        self.assertIn(
            "uv run adws/maestro.py run amend <plan> --run {0}".format(self.run_id),
            text,
        )

    def test_a_tests_stage_park_reads_test_reviews(self) -> None:
        self._review(
            101,
            findings=[_finding("tests", "claim-a")],
            failed=0,
            kind=st.ArtifactKind.TEST_REVIEW,
        )
        self._review(
            103,
            findings=[_finding("tests", "claim-a")],
            failed=0,
            kind=st.ArtifactKind.TEST_REVIEW,
        )
        self._wait(
            st.WaitReason.NO_PROGRESS, resume_stage=st.LaneStage.WRITING_TESTS
        )
        text = self.brief()
        self.assertIn("repeating", text)
        self.assertIn("Reviews 101,103", text)

    def test_pause_is_one_sentence_and_the_resume_line(self) -> None:
        self._review(101, findings=[_finding("store", "claim-one")], failed=2)
        self._wait(st.WaitReason.PAUSE)
        text = self.brief()
        self.assertIn("PAUSE", text)
        self.assertIn(
            "uv run adws/maestro.py run resume {0}".format(self.run_id), text
        )
        self.assertNotIn("run amend", text)
        self.assertNotIn("converging", text)
        self.assertNotIn("Reviews", text)
        self.assertEqual(len(text.splitlines()), 3)

    def test_amendment_required_names_the_reason_and_recommends_nothing(self) -> None:
        self._wait(st.WaitReason.AMENDMENT_REQUIRED)
        text = self.brief()
        self.assertIn("AMENDMENT_REQUIRED", text)
        self.assertIn(st.LaneStage.BUILDING.value, text)
        self.assertNotIn("uv run adws/maestro.py", text)

    def test_rendering_a_brief_writes_nothing_to_the_ledger(self) -> None:
        for sequence in (101, 103):
            self._review(sequence, findings=[_finding("store", "claim-one")], failed=2)
        self._wait(st.WaitReason.NO_PROGRESS)
        before = tuple(self.store.conn.iterdump())
        self.assertTrue(self.brief())
        self.assertEqual(tuple(self.store.conn.iterdump()), before)


def _two_lane_plan() -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": "emit a.txt",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["a.txt is written"],
            },
            {
                "id": "lane-b",
                "needs": ["lane-a"],
                "outputs": ["b.txt"],
                "spec": {
                    "goal": "emit b.txt",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["b.txt is written"],
            },
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class SecondLaneStops(ScriptedActor):
    """Merges the first lane, then interrupts inside the second lane's build."""

    def build(self, ctx: sch.LaneContext) -> dict:
        if ctx.lane.lane_id == "lane-b":
            raise KeyboardInterrupt
        return super().build(ctx)


class RunStatusPrintsTheBriefTest(SingleEntryBase):
    plan_name = "two-lane"

    def setUp(self) -> None:
        super().setUp()
        self.plan_path = ship_plan(self.repo, self.plan_name, _two_lane_plan())

    def status(self, run_id: str) -> tuple[int, Mapping[str, Any], str]:
        out, err = StringIO(), StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    maestro, "_executing_maestro_file", return_value=self.maestro_file
                )
            )
            stack.enter_context(mock.patch("sys.stdout", out))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(
                mock.patch.object(maestro, "_actor_for", return_value=object())
            )
            stack.enter_context(working_directory(self.repo))
            code = maestro.main(["run", "status", run_id])
        text = out.getvalue().strip()
        return code, json.loads(text.splitlines()[-1]), err.getvalue()

    def test_only_a_waiting_lane_gets_a_brief_and_stdout_stays_one_json_object(
        self,
    ) -> None:
        actor = SecondLaneStops(self.repo, self.state / "worktrees")
        run_id, _status = self.execute_run(actor)
        with self.ledger() as store:
            self.assertEqual(
                store.lane_stage(run_id, "lane-a"), st.LaneStage.MERGED
            )
            self.assertEqual(
                store.lane_stage(run_id, "lane-b"), st.LaneStage.WAITING_FOR_USER
            )
        code, payload, err = self.status(run_id)
        self.assertEqual(code, 0)
        self.assertEqual(payload["lanes"]["lane-b"], "WAITING_FOR_USER")
        self.assertIn("lane-b is WAITING_FOR_USER", err)
        self.assertIn(
            "uv run adws/maestro.py run resume {0}".format(run_id), err
        )
        self.assertNotIn("lane-a is WAITING_FOR_USER", err)


class NoProgressEmitsTheBriefTest(unittest.TestCase):
    """The park's own site reports the brief on the step channel."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        _init_repo(self.repo)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.addCleanup(self.runtime.close)
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        self.run_id = "run-brief-step"
        self.lane_id = "lane-a"
        self.steps: list[tuple[str, str, str]] = []

    def test_the_pause_site_reports_the_brief_it_derives(self) -> None:
        compiled = plan_compiler.compile_plan(
            _unified_plan_bytes(), plan_revision=1, plan_artifact_ref="plan:brief-step"
        )
        sch.create_factory_run(
            store=self.store,
            run_id=self.run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )
        actor = ContentActor(self.repo, self.state / "worktrees", ["a", "b", "c"])
        scheduler = sch.FactoryScheduler(
            self.store,
            self.run_id,
            actor,
            self.runtime,
            self.target,
            compiled=compiled,
            step=lambda lane, message, detail="": self.steps.append(
                (lane, message, detail)
            ),
        )
        scheduler._planned(self.lane_id)
        scheduler._writing_tests(self.lane_id)
        scheduler._reviewing_tests(self.lane_id)
        scheduler._tests_sealed(self.lane_id)
        for _ in range(3):
            scheduler._building(self.lane_id)
            scheduler._reviewing_code(self.lane_id)
        self.assertEqual(
            self.store.lane_stage(self.run_id, self.lane_id),
            st.LaneStage.WAITING_FOR_USER,
        )
        briefs = [item for item in self.steps if item[1] == "operator brief"]
        self.assertEqual(len(briefs), 1)
        lane, _message, detail = briefs[0]
        self.assertEqual(lane, self.lane_id)
        self.assertIn("is WAITING_FOR_USER", detail)
        self.assertIn(st.WaitReason.NO_PROGRESS.value, detail)
        self.assertIn(
            "uv run adws/maestro.py run resume {0}".format(self.run_id), detail
        )


if __name__ == "__main__":
    unittest.main()
