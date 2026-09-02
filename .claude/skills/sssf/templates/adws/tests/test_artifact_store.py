"""Observable artifact-store behavior for the two-lane factory kernel."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules.lifecycle import (  # noqa: E402
    AmendmentRefused,
    ArtifactCollision,
    ArtifactStore,
    LANE_ARTIFACT_COLUMNS,
    LANE_ARTIFACTS_SQL,
    LedgerSchemaUnsupported,
    ResumeBlocked,
    StageCasConflict,
    StaleStageInput,
    V1_LANE_ARTIFACTS_SQL,
    V1_SCHEMA,
)


RUN_ID = "run-1"
FINDING = {
    "implementation_area": "src/a.py",
    "observed_behavior": "returns 1",
    "required_behavior": "returns 2",
    "violated_requirement": "increment",
}


def digest_label(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def git_sha(label: str) -> str:
    return digest_label(label)[:40]


def make_lane(
    lane_id: str,
    *,
    needs: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    spec: str | None = None,
    public_acceptance: tuple[str, ...] = ("observable behavior",),
) -> st.LaneProjection:
    spec_digest = spec or digest_label(f"spec:{lane_id}")
    needs = tuple(sorted(needs))
    outputs = tuple(sorted(outputs or (f"{lane_id}.py",)))
    return st.LaneProjection(
        lane_id=lane_id,
        needs=needs,
        spec_digest=spec_digest,
        declared_outputs=outputs,
        lane_projection_digest=st.lane_projection_digest(spec_digest, needs, outputs),
        public_acceptance=public_acceptance,
    )


def make_plan(
    *lanes: st.LaneProjection, revision: int = 1, stamp: str = "v1"
) -> st.CompiledPlan:
    plan_bytes = st.canonical_bytes(
        {
            "lanes": [lane.lane_id for lane in lanes],
            "stamp": stamp,
            "revision": revision,
        }
    )
    ordered = st.topological_integration_order(lanes)
    return st.CompiledPlan(
        plan_bytes=plan_bytes,
        plan_artifact_ref=f"plans/{stamp}/{revision}.json",
        plan_digest=st.digest_bytes(plan_bytes),
        plan_revision=revision,
        lanes=lanes,
        integration_order=ordered,
    )


def make_binding(fingerprint: str | None = None) -> st.RunBinding:
    main = git_sha("main")
    return st.RunBinding(
        runtime_state_root="/tmp/maestro-state",
        runtime_state_fingerprint=fingerprint or digest_label("state"),
        integration_ref=st.integration_ref(RUN_ID),
        integration_initial_sha=main,
        target_repository_root="/tmp/product",
        target_git_common_dir="/tmp/product/.git",
        target_worktree_git_dir="/tmp/product/.git",
        target_object_format="sha1",
        target_repository_fingerprint=digest_label("repo"),
        target_sync_journal_fingerprint=digest_label("journal"),
        target_initial_main_sha=main,
        target_main_ref="refs/heads/main",
    )


def lane_artifact(
    kind: st.ArtifactKind,
    projection: st.LaneProjection,
    input_digest: str,
    payload: dict,
    *,
    revision: int = 1,
    verdict: st.ReviewerVerdict | None = None,
) -> st.LaneArtifact:
    body = dict(payload)
    body["input_digest"] = input_digest
    return st.LaneArtifact(
        kind=kind,
        plan_revision=revision,
        spec_digest=projection.spec_digest,
        lane_projection_digest=projection.lane_projection_digest,
        input_digest=input_digest,
        output_digest=st.digest_canonical(body),
        artifact_ref=f"{kind.value}:{input_digest}",
        payload=body,
        verdict=verdict,
    )


def run_artifact(
    kind: st.ArtifactKind,
    input_digest: str,
    payload: dict,
    *,
    revision: int = 1,
    verdict: st.ReviewerVerdict | None = None,
) -> st.RunArtifact:
    body = dict(payload)
    body["input_digest"] = input_digest
    return st.RunArtifact(
        kind=kind,
        plan_revision=revision,
        input_digest=input_digest,
        output_digest=st.digest_canonical(body),
        artifact_ref=f"{kind.value}:{input_digest}",
        payload=body,
        verdict=verdict,
    )


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "lifecycle.sqlite3"
        self.store = ArtifactStore(self.db)
        self.lane_a = make_lane("A")
        self.lane_b = make_lane("B", needs=("A",), outputs=("b.py",))
        self.plan = make_plan(self.lane_a, self.lane_b)
        self.store.create_run(RUN_ID, self.plan, make_binding())

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _planned_digest(self, projection: st.LaneProjection) -> str:
        return st.planned_input_digest(
            run_id=RUN_ID,
            lane_id=projection.lane_id,
            plan_revision=self.plan.plan_revision,
            plan_digest=self.plan.plan_digest,
            spec_digest=projection.spec_digest,
            projection_digest=projection.lane_projection_digest,
            plan_artifact_ref=self.plan.plan_artifact_ref,
            needs=projection.needs,
            declared_outputs=projection.declared_outputs,
        )

    def _materialize(self, projection: st.LaneProjection) -> str:
        digest = self._planned_digest(projection)
        record = self.store.complete_stage(
            RUN_ID,
            projection.lane_id,
            st.LaneStage.PLANNED,
            digest,
            lane_artifact(
                st.ArtifactKind.LANE_PLAN,
                projection,
                digest,
                {
                    "declared_outputs": list(projection.declared_outputs),
                    "input_artifact_ids": [],
                    "needs": list(projection.needs),
                    "plan_artifact_ref": self.plan.plan_artifact_ref,
                    "public_contract": {
                        "acceptance": list(projection.public_acceptance)
                    },
                },
            ),
            st.LaneStage.WRITING_TESTS,
        )
        return record.artifact_id

    def _draft(
        self,
        projection: st.LaneProjection,
        plan_id: str,
        review_id: str = st.NO_TEST_REVIEW,
        stamp: str = "d1",
    ) -> str:
        digest = st.writing_tests_input_digest(
            run_id=RUN_ID,
            lane_id=projection.lane_id,
            plan_revision=self.plan.plan_revision,
            plan_digest=self.plan.plan_digest,
            spec_digest=projection.spec_digest,
            projection_digest=projection.lane_projection_digest,
            lane_plan_id=plan_id,
            test_review_id=review_id,
            integration_head=sch.durable_integration_tip(self.store, RUN_ID),
        )
        record = self.store.complete_stage(
            RUN_ID,
            projection.lane_id,
            st.LaneStage.WRITING_TESTS,
            digest,
            lane_artifact(
                st.ArtifactKind.TEST_DRAFT,
                projection,
                digest,
                {
                    "input_artifact_ids": [plan_id, review_id],
                    "private_draft_digest": digest_label(stamp),
                    "private_draft_ref": f"vault:draft:{stamp}",
                    "public_contract": {"behavior": stamp},
                },
            ),
            st.LaneStage.REVIEWING_TESTS,
        )
        return record.artifact_id

    def _test_review(
        self,
        projection: st.LaneProjection,
        plan_id: str,
        draft_id: str,
        verdict: st.ReviewerVerdict,
    ) -> str:
        digest = st.reviewing_tests_input_digest(
            run_id=RUN_ID,
            lane_id=projection.lane_id,
            plan_revision=self.plan.plan_revision,
            plan_digest=self.plan.plan_digest,
            spec_digest=projection.spec_digest,
            projection_digest=projection.lane_projection_digest,
            lane_plan_id=plan_id,
            test_draft_id=draft_id,
        )
        next_stage = (
            st.LaneStage.TESTS_SEALED
            if verdict is st.ReviewerVerdict.PASS
            else st.LaneStage.WRITING_TESTS
        )
        payload = {
            "findings": [] if verdict is st.ReviewerVerdict.PASS else [FINDING],
            "input_artifact_ids": [plan_id, draft_id],
            "verdict": verdict.value,
        }
        record = self.store.complete_stage(
            RUN_ID,
            projection.lane_id,
            st.LaneStage.REVIEWING_TESTS,
            digest,
            lane_artifact(
                st.ArtifactKind.TEST_REVIEW,
                projection,
                digest,
                payload,
                verdict=verdict,
            ),
            next_stage,
        )
        return record.artifact_id

    def _seal(
        self,
        projection: st.LaneProjection,
        plan_id: str,
        draft_id: str,
        review_id: str,
    ) -> str:
        digest = st.tests_sealed_input_digest(
            run_id=RUN_ID,
            lane_id=projection.lane_id,
            plan_revision=self.plan.plan_revision,
            plan_digest=self.plan.plan_digest,
            spec_digest=projection.spec_digest,
            projection_digest=projection.lane_projection_digest,
            lane_plan_id=plan_id,
            test_draft_id=draft_id,
            test_review_id=review_id,
        )
        record = self.store.complete_stage(
            RUN_ID,
            projection.lane_id,
            st.LaneStage.TESTS_SEALED,
            digest,
            lane_artifact(
                st.ArtifactKind.SEALED_TEST_BUNDLE,
                projection,
                digest,
                {
                    "input_artifact_ids": [plan_id, draft_id, review_id],
                    "public_contract": {"behavior": "sealed"},
                    "vault_digest": digest_label(f"vault:{draft_id}"),
                    "vault_ref": f"vault:sealed:{draft_id}",
                },
            ),
            st.LaneStage.BUILDING,
        )
        return record.artifact_id

    def _build(
        self,
        projection: st.LaneProjection,
        *,
        entry: st.BuildingEntryKind,
        base: str,
        stamp: str,
        receipt_ids: tuple[str, ...] = (),
    ) -> str:
        plan = self.store._latest_lane_artifact(
            RUN_ID, projection.lane_id, st.ArtifactKind.LANE_PLAN
        )
        sealed = self.store._latest_lane_artifact(
            RUN_ID, projection.lane_id, st.ArtifactKind.SEALED_TEST_BUNDLE
        )
        assert plan is not None and sealed is not None
        ids = [plan["artifact_id"], sealed["artifact_id"], *receipt_ids]
        prior_builder = st.NO_PRIOR_BUILDER
        code_review = st.NO_CODE_REVIEW
        base_invalidation = st.NO_BASE_INVALIDATION
        if entry is st.BuildingEntryKind.CODE_REVISE:
            prior = self.store._latest_lane_artifact(
                RUN_ID, projection.lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            review = self.store._latest_lane_artifact(
                RUN_ID,
                projection.lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.REVISE,
            )
            assert prior is not None and review is not None
            prior_builder = prior["artifact_id"]
            code_review = review["artifact_id"]
            ids.extend([prior_builder, code_review])
        elif entry is st.BuildingEntryKind.BASE_INVALIDATION:
            invalidation = self.store._latest_lane_artifact(
                RUN_ID, projection.lane_id, st.ArtifactKind.BASE_INVALIDATION
            )
            prior = self.store._latest_lane_artifact(
                RUN_ID, projection.lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            review = self.store._latest_lane_artifact(
                RUN_ID,
                projection.lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.PASS,
            )
            assert invalidation is not None and prior is not None and review is not None
            prior_builder = prior["artifact_id"]
            code_review = review["artifact_id"]
            base_invalidation = invalidation["artifact_id"]
            ids.extend([prior_builder, code_review, base_invalidation])
        digest = st.building_input_digest(
            run_id=RUN_ID,
            lane_id=projection.lane_id,
            plan_revision=self.store._run(RUN_ID)["plan_revision"],
            plan_digest=self.store._run(RUN_ID)["plan_digest"],
            spec_digest=projection.spec_digest,
            projection_digest=projection.lane_projection_digest,
            input_artifact_ids=ids,
            entry_kind=entry,
            builder_base_sha=base,
            prior_builder=prior_builder,
            code_review=code_review,
            base_invalidation=base_invalidation,
        )
        candidate = git_sha(stamp)
        payload = {
            "builder_base_sha": base,
            "candidate_ref": st.candidate_ref(RUN_ID, projection.lane_id, digest),
            "candidate_sha": candidate,
            "changed": stamp != "zero",
            "declared_output_proof": list(projection.declared_outputs),
            "entry_kind": entry.value,
            "input_artifact_ids": ids,
            "sealed_test_digest": sealed["output_digest"],
        }
        record = self.store.complete_stage(
            RUN_ID,
            projection.lane_id,
            st.LaneStage.BUILDING,
            digest,
            lane_artifact(
                st.ArtifactKind.BUILDER_OUTPUT,
                projection,
                digest,
                payload,
                revision=self.store._run(RUN_ID)["plan_revision"],
            ),
            st.LaneStage.REVIEWING_CODE,
        )
        return record.artifact_id

    def _code_review(
        self, projection: st.LaneProjection, verdict: st.ReviewerVerdict
    ) -> str:
        run = self.store._run(RUN_ID)
        plan = self.store._latest_lane_artifact(
            RUN_ID, projection.lane_id, st.ArtifactKind.LANE_PLAN
        )
        sealed = self.store._latest_lane_artifact(
            RUN_ID, projection.lane_id, st.ArtifactKind.SEALED_TEST_BUNDLE
        )
        builder = self.store._latest_lane_artifact(
            RUN_ID, projection.lane_id, st.ArtifactKind.BUILDER_OUTPUT
        )
        assert plan is not None and sealed is not None and builder is not None
        builder_payload = builder["payload_json"]
        loaded = __import__("json").loads(builder_payload)
        digest = st.reviewing_code_input_digest(
            run_id=RUN_ID,
            lane_id=projection.lane_id,
            plan_revision=run["plan_revision"],
            plan_digest=run["plan_digest"],
            spec_digest=projection.spec_digest,
            projection_digest=projection.lane_projection_digest,
            lane_plan_id=plan["artifact_id"],
            sealed_bundle_id=sealed["artifact_id"],
            builder_output_id=builder["artifact_id"],
            builder_base_sha=loaded["builder_base_sha"],
            candidate_ref=loaded["candidate_ref"],
            candidate_sha=loaded["candidate_sha"],
        )
        next_stage = (
            st.LaneStage.READY_TO_MERGE
            if verdict is st.ReviewerVerdict.PASS
            else st.LaneStage.BUILDING
        )
        record = self.store.complete_stage(
            RUN_ID,
            projection.lane_id,
            st.LaneStage.REVIEWING_CODE,
            digest,
            lane_artifact(
                st.ArtifactKind.CODE_REVIEW,
                projection,
                digest,
                {
                    "findings": [] if verdict is st.ReviewerVerdict.PASS else [FINDING],
                    "input_artifact_ids": [
                        plan["artifact_id"],
                        sealed["artifact_id"],
                        builder["artifact_id"],
                    ],
                    "public_result_summary": "ok"
                    if verdict is st.ReviewerVerdict.PASS
                    else "revise",
                    "verdict": verdict.value,
                },
                revision=run["plan_revision"],
                verdict=verdict,
            ),
            next_stage,
        )
        return record.artifact_id

    def _merge(self, projection: st.LaneProjection, after: str) -> str:
        run = self.store._run(RUN_ID)
        builder = self.store._latest_lane_artifact(
            RUN_ID, projection.lane_id, st.ArtifactKind.BUILDER_OUTPUT
        )
        review = self.store._latest_lane_artifact(
            RUN_ID,
            projection.lane_id,
            st.ArtifactKind.CODE_REVIEW,
            verdict=st.ReviewerVerdict.PASS,
        )
        assert builder is not None and review is not None
        loaded = __import__("json").loads(builder["payload_json"])
        digest = st.ready_to_merge_input_digest(
            run_id=RUN_ID,
            lane_id=projection.lane_id,
            plan_revision=run["plan_revision"],
            plan_digest=run["plan_digest"],
            spec_digest=projection.spec_digest,
            projection_digest=projection.lane_projection_digest,
            builder_output_id=builder["artifact_id"],
            code_review_id=review["artifact_id"],
            builder_base_sha=loaded["builder_base_sha"],
            candidate_ref=loaded["candidate_ref"],
            candidate_sha=loaded["candidate_sha"],
            integration_head=loaded["builder_base_sha"],
        )
        record = self.store.complete_stage(
            RUN_ID,
            projection.lane_id,
            st.LaneStage.READY_TO_MERGE,
            digest,
            lane_artifact(
                st.ArtifactKind.INTEGRATION_MERGE,
                projection,
                digest,
                {
                    "after_sha": after,
                    "before_sha": loaded["builder_base_sha"],
                    "candidate_sha": loaded["candidate_sha"],
                    "input_artifact_ids": [
                        builder["artifact_id"],
                        review["artifact_id"],
                    ],
                    "integration_head": loaded["builder_base_sha"],
                    "revalidated": loaded["builder_base_sha"] == after,
                },
                revision=run["plan_revision"],
            ),
            st.LaneStage.MERGED,
        )
        return record.artifact_id

    def _to_sealed(self, projection: st.LaneProjection) -> tuple[str, str, str, str]:
        plan_id = self._materialize(projection)
        draft_id = self._draft(projection, plan_id)
        review_id = self._test_review(
            projection, plan_id, draft_id, st.ReviewerVerdict.PASS
        )
        sealed_id = self._seal(projection, plan_id, draft_id, review_id)
        return plan_id, draft_id, review_id, sealed_id

    def _merge_both(self) -> str:
        self._to_sealed(self.lane_a)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("main"),
            stamp="a",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.PASS)
        merge_a = self._merge(self.lane_a, git_sha("merge-a"))
        self._to_sealed(self.lane_b)
        self._build(
            self.lane_b,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("merge-a"),
            stamp="b",
            receipt_ids=(merge_a,),
        )
        self._code_review(self.lane_b, st.ReviewerVerdict.PASS)
        self._merge(self.lane_b, git_sha("merge-b"))
        return git_sha("merge-b")

    def _revise_named_a(self, integration: str) -> str:
        fingerprint = self.store.active_final_review_fingerprint(RUN_ID, integration)
        review = run_artifact(
            st.ArtifactKind.FINAL_INTEGRATION_REVIEW,
            fingerprint,
            {
                "affected_lanes": ["A"],
                "findings": [FINDING],
                "integration_sha": integration,
                "observed_target_main_sha": git_sha("main"),
                "verdict": "REVISE",
            },
            verdict=st.ReviewerVerdict.REVISE,
        )
        record = self.store.complete_final_review(
            RUN_ID,
            fingerprint,
            integration,
            git_sha("main"),
            review,
            ["A"],
        )
        return record.artifact_id

    def _amendment_artifact(
        self,
        amended: st.CompiledPlan,
        resets: tuple[st.LaneReset, ...],
        stamp: str,
        *,
        integration_head: str,
        final_review_artifact_id: str = st.NO_FINAL_REVIEW,
    ) -> st.RunArtifact:
        return run_artifact(
            st.ArtifactKind.PLAN_AMENDMENT,
            digest_label(stamp),
            {
                "final_review_artifact_id": final_review_artifact_id,
                "integration_head": integration_head,
                "invalidated_inputs": [],
                "new_plan_artifact_ref": amended.plan_artifact_ref,
                "new_plan_digest": amended.plan_digest,
                "new_plan_revision": amended.plan_revision,
                "prior_plan_digest": self.plan.plan_digest,
                "prior_plan_revision": 1,
                "projection": [lane.lane_id for lane in amended.lanes],
                "resets": [
                    {
                        "from_stage": item.from_stage.value,
                        "lane_id": item.lane_id,
                        "to_stage": item.to_stage.value,
                    }
                    for item in resets
                ],
                "retained_inputs": [],
            },
            revision=amended.plan_revision,
        )

    def test_exactly_nine_lane_stages_and_no_legacy_symbols(self) -> None:
        self.assertEqual(
            [stage.value for stage in st.LaneStage],
            [
                "PLANNED",
                "WRITING_TESTS",
                "REVIEWING_TESTS",
                "TESTS_SEALED",
                "BUILDING",
                "REVIEWING_CODE",
                "READY_TO_MERGE",
                "MERGED",
                "WAITING_FOR_USER",
            ],
        )
        self.assertFalse(hasattr(st, "NodeState"))
        self.assertFalse(hasattr(st, "LanePhase"))
        self.assertFalse(hasattr(st, "AttemptRecord"))
        self.assertEqual(
            {verdict.value for verdict in st.ReviewerVerdict}, {"PASS", "REVISE"}
        )

    def test_schema_has_one_stage_column_and_no_forbidden_tables(self) -> None:
        tables = self.store.schema_tables()
        self.assertEqual(
            tables,
            {
                "dag_lanes",
                "lane_artifacts",
                "lane_state",
                "ledger_meta",
                "plan_revisions",
                "run_artifacts",
                "runs",
                "transitions",
            },
        )
        for forbidden in (
            "attempts",
            "node_lifecycle",
            "lane_candidates",
            "candidate_reviews",
            "repair_handoffs",
            "actor_sessions",
            "dag_nodes",
        ):
            self.assertNotIn(forbidden, tables)
        columns = {
            row[1] for row in self.store.conn.execute("PRAGMA table_info(lane_state)")
        }
        self.assertEqual(columns, {"run_id", "lane_id", "stage", "updated_at"})
        dag_columns = {
            row[1] for row in self.store.conn.execute("PRAGMA table_info(dag_lanes)")
        }
        self.assertIn("public_acceptance_json", dag_columns)
        self.assertNotIn("lane_kind", dag_columns)

    def test_public_acceptance_round_trips_through_active_projection(self) -> None:
        reconstructed = self.store.active_projection(RUN_ID)
        self.assertEqual(reconstructed, (self.lane_a, self.lane_b))
        self.assertEqual(reconstructed[0].public_acceptance, ("observable behavior",))
        self.assertEqual(reconstructed[1].public_acceptance, ("observable behavior",))
        row = self.store.conn.execute(
            "SELECT public_acceptance_json FROM dag_lanes WHERE run_id=? AND lane_id=?",
            (RUN_ID, "A"),
        ).fetchone()
        self.assertEqual(
            tuple(__import__("json").loads(row[0])), ("observable behavior",)
        )

    def test_legacy_ledger_refuses_execution(self) -> None:
        path = Path(self._tmp.name) / "legacy.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE node_lifecycle(run_id TEXT, node_id TEXT, state TEXT)"
        )
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerSchemaUnsupported) as raised:
            ArtifactStore(path)
        self.assertIn("LEDGER_SCHEMA_UNSUPPORTED", str(raised.exception))

    def test_frozen_planned_canonical_vector(self) -> None:
        envelope = {
            "declared_outputs": ["A.py"],
            "input_artifact_ids": [],
            "lane_id": "A",
            "lane_projection_digest": self.lane_a.lane_projection_digest,
            "needs": [],
            "plan_artifact_ref": self.plan.plan_artifact_ref,
            "plan_digest": self.plan.plan_digest,
            "plan_revision": 1,
            "run_id": RUN_ID,
            "schema_version": 1,
            "spec_digest": self.lane_a.spec_digest,
            "stage": "PLANNED",
        }
        raw = st.canonical_bytes(envelope)
        self.assertTrue(raw.startswith(b"{"))
        self.assertNotIn(b" ", raw)
        self.assertEqual(
            st.digest_canonical(envelope), self._planned_digest(self.lane_a)
        )

    def test_author_reviewer_revise_loop_then_seal(self) -> None:
        plan_id = self._materialize(self.lane_a)
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.WRITING_TESTS)
        first_draft = self._draft(self.lane_a, plan_id, stamp="d1")
        first_review = self._test_review(
            self.lane_a, plan_id, first_draft, st.ReviewerVerdict.REVISE
        )
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.WRITING_TESTS)
        second_draft = self._draft(self.lane_a, plan_id, first_review, stamp="d2")
        self.assertNotEqual(first_draft, second_draft)
        second_review = self._test_review(
            self.lane_a, plan_id, second_draft, st.ReviewerVerdict.REVISE
        )
        self.assertNotEqual(first_review, second_review)
        third_draft = self._draft(self.lane_a, plan_id, second_review, stamp="d3")
        pass_id = self._test_review(
            self.lane_a, plan_id, third_draft, st.ReviewerVerdict.PASS
        )
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.TESTS_SEALED)
        with self.assertRaises(st.IllegalStageEdge):
            st.next_stage_for(
                st.LaneStage.TESTS_SEALED,
                st.ArtifactKind.BUILDER_OUTPUT,
                None,
            )
        sealed = self._seal(self.lane_a, plan_id, third_draft, pass_id)
        bundle = self.store.get_lane_artifact(sealed)
        self.assertNotIn("private_source", bundle.payload)
        self.assertIn("vault_digest", bundle.payload)
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.BUILDING)
        self.assertEqual(self.store.ready_lane_ids(RUN_ID), ("A",))

    def test_private_payload_keys_are_refused(self) -> None:
        with self.assertRaises(st.CanonicalIdentityError):
            lane_artifact(
                st.ArtifactKind.TEST_DRAFT,
                self.lane_a,
                digest_label("x"),
                {
                    "fixtures": ["secret"],
                    "input_digest": digest_label("x"),
                },
            )

    def test_builder_code_review_loop_and_exactly_one_merge(self) -> None:
        self._to_sealed(self.lane_a)
        base = git_sha("main")
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=base,
            stamp="c1",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.REVISE)
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.BUILDING)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.CODE_REVISE,
            base=base,
            stamp="c2",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.PASS)
        merge_id = self._merge(self.lane_a, git_sha("merge-a"))
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.MERGED)
        builder = self.store._latest_lane_artifact(
            RUN_ID, "A", st.ArtifactKind.BUILDER_OUTPUT
        )
        review = self.store._latest_lane_artifact(
            RUN_ID, "A", st.ArtifactKind.CODE_REVIEW, verdict=st.ReviewerVerdict.PASS
        )
        assert builder is not None and review is not None
        loaded = __import__("json").loads(builder["payload_json"])
        digest = st.ready_to_merge_input_digest(
            run_id=RUN_ID,
            lane_id="A",
            plan_revision=1,
            plan_digest=self.plan.plan_digest,
            spec_digest=self.lane_a.spec_digest,
            projection_digest=self.lane_a.lane_projection_digest,
            builder_output_id=builder["artifact_id"],
            code_review_id=review["artifact_id"],
            builder_base_sha=loaded["builder_base_sha"],
            candidate_ref=loaded["candidate_ref"],
            candidate_sha=loaded["candidate_sha"],
            integration_head=loaded["builder_base_sha"],
        )
        with self.assertRaises(ArtifactCollision):
            self.store.complete_stage(
                RUN_ID,
                "A",
                st.LaneStage.READY_TO_MERGE,
                digest,
                lane_artifact(
                    st.ArtifactKind.INTEGRATION_MERGE,
                    self.lane_a,
                    digest,
                    {
                        "after_sha": git_sha("other"),
                        "before_sha": loaded["builder_base_sha"],
                        "candidate_sha": loaded["candidate_sha"],
                        "input_artifact_ids": [
                            builder["artifact_id"],
                            review["artifact_id"],
                        ],
                        "integration_head": loaded["builder_base_sha"],
                        "revalidated": False,
                    },
                ),
                st.LaneStage.MERGED,
            )
        stored = self.store.get_lane_artifact(merge_id)
        replay = self.store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.READY_TO_MERGE,
            stored.input_digest,
            st.LaneArtifact(
                kind=st.ArtifactKind.INTEGRATION_MERGE,
                plan_revision=stored.plan_revision,
                spec_digest=self.lane_a.spec_digest,
                lane_projection_digest=self.lane_a.lane_projection_digest,
                input_digest=stored.input_digest,
                output_digest=stored.output_digest,
                artifact_ref=stored.artifact_ref,
                payload=stored.payload,
            ),
            st.LaneStage.MERGED,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.artifact_id, merge_id)

    def test_dependent_building_uses_highest_integration_merge(self) -> None:
        self._to_sealed(self.lane_a)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("main"),
            stamp="a",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.PASS)
        merge_id = self._merge(self.lane_a, git_sha("merge-a"))
        self._to_sealed(self.lane_b)
        self.assertEqual(self.store.ready_lane_ids(RUN_ID), ("B",))
        builder_id = self._build(
            self.lane_b,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("merge-a"),
            stamp="b",
            receipt_ids=(merge_id,),
        )
        payload = self.store.get_lane_artifact(builder_id).payload
        self.assertIn(merge_id, payload["input_artifact_ids"])

    def test_exact_replay_and_illegal_edge(self) -> None:
        digest = self._planned_digest(self.lane_a)
        artifact = lane_artifact(
            st.ArtifactKind.LANE_PLAN,
            self.lane_a,
            digest,
            {
                "declared_outputs": ["A.py"],
                "input_artifact_ids": [],
                "needs": [],
                "plan_artifact_ref": self.plan.plan_artifact_ref,
            },
        )
        first = self.store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.PLANNED,
            digest,
            artifact,
            st.LaneStage.WRITING_TESTS,
        )
        second = self.store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.PLANNED,
            digest,
            artifact,
            st.LaneStage.WRITING_TESTS,
        )
        self.assertTrue(second.replayed)
        self.assertEqual(first.artifact_id, second.artifact_id)
        rows = list(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM lane_artifacts WHERE lane_id='A'"
            )
        )
        self.assertEqual(rows[0][0], 1)
        with self.assertRaises(st.IllegalStageEdge):
            st.next_stage_for(
                st.LaneStage.PLANNED, st.ArtifactKind.BUILDER_OUTPUT, None
            )

    def test_exact_replay_when_expected_stage_differs_from_current(self) -> None:
        digest = self._planned_digest(self.lane_a)
        artifact = lane_artifact(
            st.ArtifactKind.LANE_PLAN,
            self.lane_a,
            digest,
            {
                "declared_outputs": ["A.py"],
                "input_artifact_ids": [],
                "needs": [],
                "plan_artifact_ref": self.plan.plan_artifact_ref,
            },
        )
        first = self.store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.PLANNED,
            digest,
            artifact,
            st.LaneStage.WRITING_TESTS,
        )
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.WRITING_TESTS)
        replay = self.store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.BUILDING,
            digest,
            artifact,
            st.LaneStage.WRITING_TESTS,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.artifact_id, replay.artifact_id)
        rows = list(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM lane_artifacts WHERE lane_id='A'"
            )
        )
        self.assertEqual(rows[0][0], 1)

    def test_same_lane_cas_race_one_winner(self) -> None:
        digest = self._planned_digest(self.lane_a)
        artifact = lane_artifact(
            st.ArtifactKind.LANE_PLAN,
            self.lane_a,
            digest,
            {
                "declared_outputs": ["A.py"],
                "input_artifact_ids": [],
                "needs": [],
                "plan_artifact_ref": self.plan.plan_artifact_ref,
            },
        )
        barrier = threading.Barrier(2)
        results: list[object] = []

        def worker() -> None:
            barrier.wait()
            try:
                results.append(
                    self.store.complete_stage(
                        RUN_ID,
                        "A",
                        st.LaneStage.PLANNED,
                        digest,
                        artifact,
                        st.LaneStage.WRITING_TESTS,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker), pool.submit(worker)]
            for future in futures:
                future.result()
        records = [item for item in results if isinstance(item, type(results[0]))]
        winners = [item for item in results if getattr(item, "artifact_id", None)]
        self.assertEqual(len(winners), 2)
        self.assertEqual(winners[0].artifact_id, winners[1].artifact_id)
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM lane_artifacts WHERE lane_id='A'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.WRITING_TESTS)
        del records

    def test_planned_pause_no_predecessor_and_second_pause_is_distinct(self) -> None:
        digest = self._planned_digest(self.lane_a)
        first = self.store.pause_lane(RUN_ID, "A", st.LaneStage.PLANNED, digest)
        self.assertEqual(first.payload["predecessor_artifact_id"], st.NO_PREDECESSOR)
        self.assertEqual(first.payload["predecessor_sequence"], 0)
        replay = self.store.pause_lane(RUN_ID, "A", st.LaneStage.PLANNED, digest)
        self.assertTrue(replay.replayed)
        resumed = self.store.resume_lane(RUN_ID, "A")
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.PLANNED)
        second = self.store.pause_lane(RUN_ID, "A", st.LaneStage.PLANNED, digest)
        self.assertFalse(second.replayed)
        self.assertEqual(second.payload["predecessor_artifact_id"], resumed.artifact_id)
        self.assertNotEqual(first.artifact_id, second.artifact_id)

    def test_amendment_required_ignores_bare_resume(self) -> None:
        self._to_sealed(self.lane_a)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("main"),
            stamp="a",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.PASS)
        merge_a = self._merge(self.lane_a, git_sha("merge-a"))
        self._to_sealed(self.lane_b)
        self._build(
            self.lane_b,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("merge-a"),
            stamp="b",
            receipt_ids=(merge_a,),
        )
        self._code_review(self.lane_b, st.ReviewerVerdict.PASS)
        self._merge(self.lane_b, git_sha("merge-b"))
        integration = git_sha("merge-b")
        fingerprint = self.store.active_final_review_fingerprint(RUN_ID, integration)
        review = run_artifact(
            st.ArtifactKind.FINAL_INTEGRATION_REVIEW,
            fingerprint,
            {
                "affected_lanes": ["A"],
                "findings": [FINDING],
                "integration_sha": integration,
                "observed_target_main_sha": git_sha("main"),
                "verdict": "REVISE",
            },
            verdict=st.ReviewerVerdict.REVISE,
        )
        self.store.complete_final_review(
            RUN_ID,
            fingerprint,
            integration,
            git_sha("main"),
            review,
            ["A"],
        )
        self.assertEqual(
            self.store.lane_stage(RUN_ID, "A"), st.LaneStage.WAITING_FOR_USER
        )
        blocked = self.store.resume_lane(RUN_ID, "A")
        self.assertTrue(blocked.replayed)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, "A"), st.LaneStage.WAITING_FOR_USER
        )
        self.assertEqual(
            self.store.derive_run_status(RUN_ID, integration), st.RunStatus.WAITING
        )

    def test_named_waiting_lane_refuses_topology_change(self) -> None:
        integration = self._merge_both()
        review_id = self._revise_named_a(integration)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, "A"), st.LaneStage.WAITING_FOR_USER
        )
        moved = make_lane("A", outputs=("A-moved.py",))
        self.assertEqual(moved.spec_digest, self.lane_a.spec_digest)
        amended = make_plan(moved, self.lane_b, revision=2, stamp="named-topo")
        resets = (
            st.LaneReset("A", st.LaneStage.WAITING_FOR_USER, st.LaneStage.PLANNED),
            st.LaneReset("B", st.LaneStage.MERGED, st.LaneStage.BUILDING),
        )
        with self.assertRaises(AmendmentRefused) as raised:
            self.store.apply_amendment(
                RUN_ID,
                1,
                amended,
                self._amendment_artifact(
                    amended,
                    resets,
                    "named-topo",
                    integration_head=integration,
                    final_review_artifact_id=review_id,
                ),
                resets,
            )
        self.assertIn("merged needs/output", str(raised.exception))
        spec_and_outputs = make_lane(
            "A", spec=digest_label("spec:A:review"), outputs=("A-moved.py",)
        )
        mixed = make_plan(
            spec_and_outputs, self.lane_b, revision=2, stamp="named-mixed"
        )
        with self.assertRaises(AmendmentRefused) as mixed_raised:
            self.store.apply_amendment(
                RUN_ID,
                1,
                mixed,
                self._amendment_artifact(
                    mixed,
                    resets,
                    "named-mixed",
                    integration_head=integration,
                    final_review_artifact_id=review_id,
                ),
                resets,
            )
        self.assertIn("merged needs/output", str(mixed_raised.exception))

    def test_named_waiting_lane_requires_spec_digest(self) -> None:
        integration = self._merge_both()
        review_id = self._revise_named_a(integration)
        unchanged = make_plan(self.lane_a, self.lane_b, revision=2, stamp="named-same")
        resets = (
            st.LaneReset("A", st.LaneStage.WAITING_FOR_USER, st.LaneStage.PLANNED),
            st.LaneReset("B", st.LaneStage.MERGED, st.LaneStage.BUILDING),
        )
        with self.assertRaises(AmendmentRefused) as raised:
            self.store.apply_amendment(
                RUN_ID,
                1,
                unchanged,
                self._amendment_artifact(
                    unchanged,
                    resets,
                    "named-same",
                    integration_head=integration,
                    final_review_artifact_id=review_id,
                ),
                resets,
            )
        self.assertIn("AMENDMENT_DOES_NOT_ADDRESS_REVIEW", str(raised.exception))

    def test_named_waiting_lane_spec_change_resets_planned(self) -> None:
        integration = self._merge_both()
        review_id = self._revise_named_a(integration)
        changed_a = make_lane("A", spec=digest_label("spec:A:review"))
        self.assertEqual(changed_a.needs, self.lane_a.needs)
        self.assertEqual(changed_a.declared_outputs, self.lane_a.declared_outputs)
        self.assertNotEqual(changed_a.spec_digest, self.lane_a.spec_digest)
        self.assertNotEqual(
            changed_a.lane_projection_digest, self.lane_a.lane_projection_digest
        )
        amended = make_plan(changed_a, self.lane_b, revision=2, stamp="named-spec")
        resets = (
            st.LaneReset("A", st.LaneStage.WAITING_FOR_USER, st.LaneStage.PLANNED),
            st.LaneReset("B", st.LaneStage.MERGED, st.LaneStage.TESTS_SEALED),
        )
        self.store.apply_amendment(
            RUN_ID,
            1,
            amended,
            self._amendment_artifact(
                amended,
                resets,
                "named-spec",
                integration_head=integration,
                final_review_artifact_id=review_id,
            ),
            resets,
        )
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.PLANNED)
        self.assertEqual(self.store.lane_stage(RUN_ID, "B"), st.LaneStage.TESTS_SEALED)
        reconstructed = {
            lane.lane_id: lane for lane in self.store.active_projection(RUN_ID)
        }
        self.assertEqual(reconstructed["A"].spec_digest, changed_a.spec_digest)
        self.assertEqual(
            reconstructed["A"].lane_projection_digest,
            changed_a.lane_projection_digest,
        )
        self.assertEqual(reconstructed["A"].needs, self.lane_a.needs)
        self.assertEqual(
            reconstructed["A"].declared_outputs, self.lane_a.declared_outputs
        )

    def test_an_answered_review_stops_naming_lanes_for_later_amendments(self) -> None:
        """A REVISE binds the amendment that answers it, and no others.

        On run f50638ab the final review named two lanes, the amendment that
        addressed it landed, and both lanes went on to MERGE. Every later
        amendment was still refused `AMENDMENT_DOES_NOT_ADDRESS_REVIEW` for not
        re-editing those two -- which by then were merged and correct, so the
        only way to satisfy the check was to damage finished work. The lane
        that actually needed amending was a third one the review never named.
        """
        integration = self._merge_both()
        review_id = self._revise_named_a(integration)

        changed_a = make_lane("A", spec=digest_label("spec:A:review"))
        first = make_plan(changed_a, self.lane_b, revision=2, stamp="answered-first")
        first_resets = (
            st.LaneReset("A", st.LaneStage.WAITING_FOR_USER, st.LaneStage.PLANNED),
            st.LaneReset("B", st.LaneStage.MERGED, st.LaneStage.TESTS_SEALED),
        )
        self.store.apply_amendment(
            RUN_ID,
            1,
            first,
            self._amendment_artifact(
                first,
                first_resets,
                "answered-first",
                integration_head=integration,
                final_review_artifact_id=review_id,
            ),
            first_resets,
        )

        # The second amendment leaves A exactly as the first one left it and
        # touches only B. Under the stale lookup this was refused.
        changed_b = make_lane("B", spec=digest_label("spec:B:second"))
        second = make_plan(changed_a, changed_b, revision=3, stamp="answered-second")
        second_resets = (
            st.LaneReset("A", st.LaneStage.PLANNED, st.LaneStage.PLANNED),
            st.LaneReset("B", st.LaneStage.TESTS_SEALED, st.LaneStage.PLANNED),
        )
        artifact = run_artifact(
            st.ArtifactKind.PLAN_AMENDMENT,
            digest_label("answered-second"),
            {
                "final_review_artifact_id": st.NO_FINAL_REVIEW,
                "integration_head": integration,
                "invalidated_inputs": [],
                "new_plan_artifact_ref": second.plan_artifact_ref,
                "new_plan_digest": second.plan_digest,
                "new_plan_revision": second.plan_revision,
                "prior_plan_digest": first.plan_digest,
                "prior_plan_revision": 2,
                "projection": [lane.lane_id for lane in second.lanes],
                "resets": [
                    {
                        "from_stage": item.from_stage.value,
                        "lane_id": item.lane_id,
                        "to_stage": item.to_stage.value,
                    }
                    for item in second_resets
                ],
                "retained_inputs": [],
            },
            revision=3,
        )
        self.store.apply_amendment(RUN_ID, 2, second, artifact, second_resets)

        reconstructed = {
            lane.lane_id: lane for lane in self.store.active_projection(RUN_ID)
        }
        self.assertEqual(reconstructed["A"].spec_digest, changed_a.spec_digest)
        self.assertEqual(reconstructed["B"].spec_digest, changed_b.spec_digest)
        self.assertEqual(self.store.lane_stage(RUN_ID, "B"), st.LaneStage.PLANNED)

    def test_an_unanswered_review_still_names_its_lanes(self) -> None:
        # The freshness test must not make the obligation vanish. With no
        # amendment recorded after the review, the named lane is still bound.
        integration = self._merge_both()
        review_id = self._revise_named_a(integration)
        unchanged = make_plan(self.lane_a, self.lane_b, revision=2, stamp="still-named")
        resets = (
            st.LaneReset("A", st.LaneStage.WAITING_FOR_USER, st.LaneStage.PLANNED),
            st.LaneReset("B", st.LaneStage.MERGED, st.LaneStage.BUILDING),
        )
        with self.assertRaises(AmendmentRefused) as raised:
            self.store.apply_amendment(
                RUN_ID,
                1,
                unchanged,
                self._amendment_artifact(
                    unchanged,
                    resets,
                    "still-named",
                    integration_head=integration,
                    final_review_artifact_id=review_id,
                ),
                resets,
            )
        self.assertIn("AMENDMENT_DOES_NOT_ADDRESS_REVIEW", str(raised.exception))

    def test_base_invalidation_returns_to_building_without_pause(self) -> None:
        self._to_sealed(self.lane_a)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("main"),
            stamp="zero",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.PASS)
        run = self.store._run(RUN_ID)
        builder = self.store._latest_lane_artifact(
            RUN_ID, "A", st.ArtifactKind.BUILDER_OUTPUT
        )
        review = self.store._latest_lane_artifact(
            RUN_ID, "A", st.ArtifactKind.CODE_REVIEW, verdict=st.ReviewerVerdict.PASS
        )
        assert builder is not None and review is not None
        loaded = __import__("json").loads(builder["payload_json"])
        new_head = git_sha("advanced")
        digest = st.base_invalidation_input_digest(
            run_id=RUN_ID,
            lane_id="A",
            plan_revision=run["plan_revision"],
            plan_digest=run["plan_digest"],
            spec_digest=self.lane_a.spec_digest,
            projection_digest=self.lane_a.lane_projection_digest,
            builder_output_id=builder["artifact_id"],
            code_review_id=review["artifact_id"],
            stale_builder_base_sha=loaded["builder_base_sha"],
            stale_candidate_sha=loaded["candidate_sha"],
            integration_head=new_head,
        )
        self.store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.READY_TO_MERGE,
            digest,
            lane_artifact(
                st.ArtifactKind.BASE_INVALIDATION,
                self.lane_a,
                digest,
                {
                    "input_artifact_ids": [
                        builder["artifact_id"],
                        review["artifact_id"],
                    ],
                    "integration_head": new_head,
                    "stale_builder_base_sha": loaded["builder_base_sha"],
                    "stale_candidate_sha": loaded["candidate_sha"],
                },
            ),
            st.LaneStage.BUILDING,
        )
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.BUILDING)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.BASE_INVALIDATION,
            base=new_head,
            stamp="rebuilt",
        )

    def test_amendment_stale_building_input_and_policy_resets(self) -> None:
        self._to_sealed(self.lane_a)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("main"),
            stamp="a",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.PASS)
        merge_a = self._merge(self.lane_a, git_sha("merge-a"))
        plan_b = self._materialize(self.lane_b)
        self._draft(self.lane_b, plan_b)
        changed_a = make_lane("A", spec=digest_label("spec:A:changed"))
        unchanged_b = self.lane_b
        new_c = make_lane("C", needs=("B",), outputs=("c.py",))
        amended = make_plan(changed_a, unchanged_b, new_c, revision=2, stamp="v2")
        resets = (
            st.LaneReset("A", st.LaneStage.MERGED, st.LaneStage.PLANNED),
            st.LaneReset(
                "B", st.LaneStage.REVIEWING_TESTS, st.LaneStage.REVIEWING_TESTS
            ),
            st.LaneReset("C", st.LaneStage.PLANNED, st.LaneStage.PLANNED),
        )
        payload = {
            "final_review_artifact_id": st.NO_FINAL_REVIEW,
            "integration_head": git_sha("merge-a"),
            "invalidated_inputs": [],
            "new_plan_artifact_ref": amended.plan_artifact_ref,
            "new_plan_digest": amended.plan_digest,
            "new_plan_revision": 2,
            "prior_plan_digest": self.plan.plan_digest,
            "prior_plan_revision": 1,
            "projection": [lane.lane_id for lane in amended.lanes],
            "resets": [
                {
                    "from_stage": item.from_stage.value,
                    "lane_id": item.lane_id,
                    "to_stage": item.to_stage.value,
                }
                for item in resets
            ],
            "retained_inputs": [],
        }
        amendment = run_artifact(
            st.ArtifactKind.PLAN_AMENDMENT,
            digest_label("amend-1"),
            payload,
            revision=2,
        )
        self.store.apply_amendment(RUN_ID, 1, amended, amendment, resets)
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.PLANNED)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, "B"), st.LaneStage.REVIEWING_TESTS
        )
        self.assertEqual(self.store.lane_stage(RUN_ID, "C"), st.LaneStage.PLANNED)
        self.assertEqual(self.store.ready_lane_ids(RUN_ID), ("A",))
        del merge_a

    def test_stale_stage_input_when_revision_not_retained(self) -> None:
        self._to_sealed(self.lane_a)
        digest_before = self.store.lane_stage(RUN_ID, "A")
        self.assertEqual(digest_before, st.LaneStage.BUILDING)
        old_plan = self.lane_a
        changed = make_lane("A", spec=digest_label("spec:A:v2"))
        b = self.lane_b
        amended = make_plan(changed, b, revision=2, stamp="v2")
        resets = (
            st.LaneReset("A", st.LaneStage.BUILDING, st.LaneStage.PLANNED),
            st.LaneReset("B", st.LaneStage.PLANNED, st.LaneStage.PLANNED),
        )
        amendment = run_artifact(
            st.ArtifactKind.PLAN_AMENDMENT,
            digest_label("amend-stale"),
            {
                "final_review_artifact_id": st.NO_FINAL_REVIEW,
                "integration_head": git_sha("main"),
                "invalidated_inputs": [],
                "new_plan_artifact_ref": amended.plan_artifact_ref,
                "new_plan_digest": amended.plan_digest,
                "new_plan_revision": 2,
                "prior_plan_digest": self.plan.plan_digest,
                "prior_plan_revision": 1,
                "projection": ["A", "B"],
                "resets": [
                    {
                        "from_stage": item.from_stage.value,
                        "lane_id": item.lane_id,
                        "to_stage": item.to_stage.value,
                    }
                    for item in resets
                ],
                "retained_inputs": [],
            },
            revision=2,
        )
        self.store.apply_amendment(RUN_ID, 1, amended, amendment, resets)
        plan = self.store._latest_lane_artifact(RUN_ID, "A", st.ArtifactKind.LANE_PLAN)
        sealed = self.store._latest_lane_artifact(
            RUN_ID, "A", st.ArtifactKind.SEALED_TEST_BUNDLE
        )
        assert plan is not None and sealed is not None
        ids = [plan["artifact_id"], sealed["artifact_id"]]
        digest = st.building_input_digest(
            run_id=RUN_ID,
            lane_id="A",
            plan_revision=1,
            plan_digest=self.plan.plan_digest,
            spec_digest=old_plan.spec_digest,
            projection_digest=old_plan.lane_projection_digest,
            input_artifact_ids=ids,
            entry_kind=st.BuildingEntryKind.INITIAL,
            builder_base_sha=git_sha("main"),
            prior_builder=st.NO_PRIOR_BUILDER,
            code_review=st.NO_CODE_REVIEW,
            base_invalidation=st.NO_BASE_INVALIDATION,
        )
        with self.assertRaises(StaleStageInput):
            self.store.complete_stage(
                RUN_ID,
                "A",
                st.LaneStage.BUILDING,
                digest,
                lane_artifact(
                    st.ArtifactKind.BUILDER_OUTPUT,
                    old_plan,
                    digest,
                    {
                        "builder_base_sha": git_sha("main"),
                        "candidate_ref": st.candidate_ref(RUN_ID, "A", digest),
                        "candidate_sha": git_sha("old"),
                        "changed": True,
                        "declared_output_proof": ["A.py"],
                        "entry_kind": "INITIAL",
                        "input_artifact_ids": ids,
                        "sealed_test_digest": sealed["output_digest"],
                    },
                    revision=1,
                ),
                st.LaneStage.REVIEWING_CODE,
            )

    def test_merged_needs_change_refused_and_publication_blocks_amendment(self) -> None:
        self._to_sealed(self.lane_a)
        self._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("main"),
            stamp="a",
        )
        self._code_review(self.lane_a, st.ReviewerVerdict.PASS)
        merge_a = self._merge(self.lane_a, git_sha("merge-a"))
        self._to_sealed(self.lane_b)
        self._build(
            self.lane_b,
            entry=st.BuildingEntryKind.INITIAL,
            base=git_sha("merge-a"),
            stamp="b",
            receipt_ids=(merge_a,),
        )
        self._code_review(self.lane_b, st.ReviewerVerdict.PASS)
        self._merge(self.lane_b, git_sha("merge-b"))
        moved = make_lane("B", needs=("A",), outputs=("b-moved.py",))
        amended = make_plan(self.lane_a, moved, revision=2, stamp="bad")
        with self.assertRaises(AmendmentRefused):
            self.store.apply_amendment(
                RUN_ID,
                1,
                amended,
                run_artifact(
                    st.ArtifactKind.PLAN_AMENDMENT,
                    digest_label("bad"),
                    {
                        "final_review_artifact_id": st.NO_FINAL_REVIEW,
                        "integration_head": git_sha("merge-b"),
                        "invalidated_inputs": [],
                        "new_plan_artifact_ref": amended.plan_artifact_ref,
                        "new_plan_digest": amended.plan_digest,
                        "new_plan_revision": 2,
                        "prior_plan_digest": self.plan.plan_digest,
                        "prior_plan_revision": 1,
                        "projection": ["A", "B"],
                        "resets": [],
                        "retained_inputs": [],
                    },
                    revision=2,
                ),
                (
                    st.LaneReset("A", st.LaneStage.MERGED, st.LaneStage.MERGED),
                    st.LaneReset("B", st.LaneStage.MERGED, st.LaneStage.PLANNED),
                ),
            )
        integration = git_sha("merge-b")
        fingerprint = self.store.active_final_review_fingerprint(RUN_ID, integration)
        self.assertEqual(
            self.store.derive_run_status(RUN_ID, integration),
            st.RunStatus.INTEGRATION_REVIEW_PENDING,
        )
        review = run_artifact(
            st.ArtifactKind.FINAL_INTEGRATION_REVIEW,
            fingerprint,
            {
                "affected_lanes": [],
                "findings": [],
                "integration_sha": integration,
                "observed_target_main_sha": git_sha("main"),
                "verdict": "PASS",
            },
            verdict=st.ReviewerVerdict.PASS,
        )
        self.store.complete_final_review(
            RUN_ID,
            fingerprint,
            integration,
            git_sha("main"),
            review,
            (),
        )
        self.assertEqual(
            self.store.derive_run_status(RUN_ID, integration), st.RunStatus.PUBLISHABLE
        )
        publication = run_artifact(
            st.ArtifactKind.MAIN_PUBLICATION,
            fingerprint,
            {
                "expected_before_sha": git_sha("main"),
                "published_sha": integration,
                "receipt_object": digest_label("receipt"),
                "receipt_ref": st.publication_ref(RUN_ID, fingerprint),
            },
        )
        first = self.store.complete_publication(
            RUN_ID,
            fingerprint,
            st.publication_ref(RUN_ID, fingerprint),
            digest_label("receipt"),
            git_sha("main"),
            integration,
            publication,
        )
        replay = self.store.complete_publication(
            RUN_ID,
            fingerprint,
            st.publication_ref(RUN_ID, fingerprint),
            digest_label("receipt"),
            git_sha("main"),
            integration,
            publication,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.artifact_id, replay.artifact_id)
        self.assertEqual(
            self.store.derive_run_status(RUN_ID, integration), st.RunStatus.COMPLETE
        )
        with self.assertRaises(AmendmentRefused):
            self.store.apply_amendment(
                RUN_ID,
                1,
                make_plan(
                    make_lane("A", spec=digest_label("later")),
                    self.lane_b,
                    revision=2,
                    stamp="late",
                ),
                run_artifact(
                    st.ArtifactKind.PLAN_AMENDMENT,
                    digest_label("late"),
                    {
                        "final_review_artifact_id": st.NO_FINAL_REVIEW,
                        "integration_head": integration,
                        "invalidated_inputs": [],
                        "new_plan_artifact_ref": "x",
                        "new_plan_digest": digest_label("x"),
                        "new_plan_revision": 2,
                        "prior_plan_digest": self.plan.plan_digest,
                        "prior_plan_revision": 1,
                        "projection": ["A", "B"],
                        "resets": [],
                        "retained_inputs": [],
                    },
                    revision=2,
                ),
                (),
            )

    def test_death_before_commit_does_not_advance_stage(self) -> None:
        digest = self._planned_digest(self.lane_a)
        artifact = lane_artifact(
            st.ArtifactKind.LANE_PLAN,
            self.lane_a,
            digest,
            {
                "declared_outputs": ["A.py"],
                "input_artifact_ids": [],
                "needs": [],
                "plan_artifact_ref": self.plan.plan_artifact_ref,
            },
        )
        self.store._begin()
        try:
            raise RuntimeError("killed")
        except RuntimeError:
            self.store.conn.execute("ROLLBACK")
        self.assertEqual(self.store.lane_stage(RUN_ID, "A"), st.LaneStage.PLANNED)
        record = self.store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.PLANNED,
            digest,
            artifact,
            st.LaneStage.WRITING_TESTS,
        )
        self.assertFalse(record.replayed)

    def test_resume_without_wait_is_blocked(self) -> None:
        with self.assertRaises(ResumeBlocked):
            self.store.resume_lane(RUN_ID, "A")


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split())


def _schema_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT schema_version FROM ledger_meta").fetchone()
    return None if row is None else row[0]


def _lane_artifact_rows(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM lane_artifacts ORDER BY run_id, lane_id, sequence, artifact_id"
        )
    ]


def _copy_into_v1(src_conn: sqlite3.Connection, dest_path: Path) -> None:
    dest = sqlite3.connect(dest_path, isolation_level=None)
    try:
        dest.executescript(V1_SCHEMA)
        dest.execute(
            "INSERT INTO ledger_meta(schema_version) VALUES (?)",
            (st.LEDGER_SCHEMA_VERSION_V1,),
        )
        dest.execute("PRAGMA foreign_keys=ON")
        dest.execute("PRAGMA defer_foreign_keys=ON")
        dest.execute("BEGIN")
        for table in (
            "plan_revisions",
            "runs",
            "dag_lanes",
            "lane_state",
            "lane_artifacts",
            "run_artifacts",
            "transitions",
        ):
            rows = [tuple(row) for row in src_conn.execute(f"SELECT * FROM {table}")]
            if not rows:
                continue
            placeholders = ",".join("?" * len(rows[0]))
            dest.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        dest.execute("COMMIT")
    except Exception:
        try:
            dest.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        dest.close()


class LedgerSchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._fx = ArtifactStoreTests()
        self._fx.setUp()
        self.addCleanup(self._fx.tearDown)
        self.store = self._fx.store
        self.lane_a = self._fx.lane_a
        self._tmp = self._fx._tmp

    def _advance_a_to_reviewing_code(self) -> None:
        self._fx._to_sealed(self.lane_a)
        self._fx._build(
            self.lane_a,
            entry=st.BuildingEntryKind.INITIAL,
            base=self.store._run(RUN_ID)["integration_initial_sha"],
            stamp="cand-1",
        )

    def _complete_test_invalidation(self, store: ArtifactStore) -> None:
        run = store._run(RUN_ID)
        plan = store._latest_lane_artifact(RUN_ID, "A", st.ArtifactKind.LANE_PLAN)
        sealed = store._latest_lane_artifact(
            RUN_ID, "A", st.ArtifactKind.SEALED_TEST_BUNDLE
        )
        builder = store._latest_lane_artifact(
            RUN_ID, "A", st.ArtifactKind.BUILDER_OUTPUT
        )
        assert plan is not None and sealed is not None and builder is not None
        loaded = json.loads(builder["payload_json"])
        digest = st.reviewing_code_input_digest(
            run_id=RUN_ID,
            lane_id="A",
            plan_revision=run["plan_revision"],
            plan_digest=run["plan_digest"],
            spec_digest=self.lane_a.spec_digest,
            projection_digest=self.lane_a.lane_projection_digest,
            lane_plan_id=plan["artifact_id"],
            sealed_bundle_id=sealed["artifact_id"],
            builder_output_id=builder["artifact_id"],
            builder_base_sha=loaded["builder_base_sha"],
            candidate_ref=loaded["candidate_ref"],
            candidate_sha=loaded["candidate_sha"],
        )
        payload = {
            "code": "PRIVATE_PATH_COLLISION",
            "input_artifact_ids": [
                plan["artifact_id"],
                sealed["artifact_id"],
                builder["artifact_id"],
            ],
            "kind": st.ArtifactKind.TEST_INVALIDATION.value,
            "reason": {
                "implementation_area": "private test suite",
                "observed_behavior": (
                    "sealed private path collides with candidate: A.py"
                ),
                "required_behavior": "private tests must be hidden",
                "violated_requirement": "private tester paths must not collide",
            },
            "schema_version": st.CANONICAL_SCHEMA_VERSION,
        }
        record = store.complete_stage(
            RUN_ID,
            "A",
            st.LaneStage.REVIEWING_CODE,
            digest,
            lane_artifact(
                st.ArtifactKind.TEST_INVALIDATION,
                self.lane_a,
                digest,
                payload,
                revision=run["plan_revision"],
            ),
            st.LaneStage.WRITING_TESTS,
        )
        self.assertFalse(record.replayed)
        self.assertEqual(store.lane_stage(RUN_ID, "A"), st.LaneStage.WRITING_TESTS)

    def test_v1_ledger_migrates_to_v2_preserving_rows_then_accepts_test_invalidation(
        self,
    ) -> None:
        self._advance_a_to_reviewing_code()
        before = _lane_artifact_rows(self.store.conn)
        self.assertGreaterEqual(len(before), 5)
        dest = Path(self._tmp.name) / "v1.sqlite3"
        _copy_into_v1(self.store.conn, dest)
        probe = sqlite3.connect(dest)
        try:
            self.assertEqual(_schema_version(probe), st.LEDGER_SCHEMA_VERSION_V1)
            sql = probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
            ).fetchone()[0]
            self.assertEqual(_normalize_sql(sql), _normalize_sql(V1_LANE_ARTIFACTS_SQL))
            self.assertNotIn("TEST_INVALIDATION", sql)
            self.assertEqual(_lane_artifact_rows(probe), before)
            with self.assertRaises(sqlite3.IntegrityError):
                probe.execute(
                    "INSERT INTO lane_artifacts("
                    + ",".join(LANE_ARTIFACT_COLUMNS)
                    + ") VALUES ("
                    + ",".join("?" * len(LANE_ARTIFACT_COLUMNS))
                    + ")",
                    (
                        "ab" * 32,
                        RUN_ID,
                        "A",
                        999,
                        "REVIEWING_CODE",
                        "TEST_INVALIDATION",
                        1,
                        self.lane_a.spec_digest,
                        self.lane_a.lane_projection_digest,
                        "cd" * 32,
                        "ef" * 32,
                        "ref",
                        "{}",
                        "2020-01-01T00:00:00Z",
                    ),
                )
        finally:
            probe.close()

        migrated = ArtifactStore(dest)
        self.addCleanup(migrated.close)
        self.assertEqual(st.LEDGER_SCHEMA_VERSION, "artifact-factory.v2")
        self.assertEqual(_schema_version(migrated.conn), st.LEDGER_SCHEMA_VERSION)
        after_sql = migrated.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
        ).fetchone()[0]
        self.assertIn("TEST_INVALIDATION", after_sql)
        self.assertEqual(
            _normalize_sql(after_sql), _normalize_sql(LANE_ARTIFACTS_SQL)
        )
        self.assertEqual(_lane_artifact_rows(migrated.conn), before)
        self.assertEqual(list(migrated.conn.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual(
            migrated.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        self._complete_test_invalidation(migrated)
        invalidations = list(
            migrated.conn.execute(
                "SELECT artifact_kind FROM lane_artifacts "
                "WHERE artifact_kind='TEST_INVALIDATION'"
            )
        )
        self.assertEqual(len(invalidations), 1)

    def test_migrated_v2_reopen_is_idempotent(self) -> None:
        self._advance_a_to_reviewing_code()
        dest = Path(self._tmp.name) / "v1-idempotent.sqlite3"
        _copy_into_v1(self.store.conn, dest)
        first = ArtifactStore(dest)
        sql1 = first.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
        ).fetchone()[0]
        rows1 = _lane_artifact_rows(first.conn)
        version1 = _schema_version(first.conn)
        first.close()
        second = ArtifactStore(dest)
        self.addCleanup(second.close)
        sql2 = second.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
        ).fetchone()[0]
        self.assertEqual(sql2, sql1)
        self.assertEqual(_lane_artifact_rows(second.conn), rows1)
        self.assertEqual(_schema_version(second.conn), version1)
        self.assertEqual(version1, st.LEDGER_SCHEMA_VERSION)

    def test_malformed_v1_shape_is_refused_unmodified(self) -> None:
        dest = Path(self._tmp.name) / "bad-v1.sqlite3"
        conn = sqlite3.connect(dest)
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO ledger_meta(schema_version) VALUES (?)",
            (st.LEDGER_SCHEMA_VERSION_V1,),
        )
        conn.execute("ALTER TABLE lane_artifacts ADD COLUMN extra TEXT")
        conn.commit()
        sql_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
        ).fetchone()[0]
        version_before = _schema_version(conn)
        conn.close()
        with self.assertRaises(LedgerSchemaUnsupported):
            ArtifactStore(dest)
        probe = sqlite3.connect(dest)
        try:
            self.assertEqual(_schema_version(probe), version_before)
            sql_after = probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
            ).fetchone()[0]
            self.assertEqual(sql_after, sql_before)
            self.assertIn("extra", sql_after)
        finally:
            probe.close()

    def test_unknown_schema_version_is_refused(self) -> None:
        dest = Path(self._tmp.name) / "v9.sqlite3"
        conn = sqlite3.connect(dest)
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO ledger_meta(schema_version) VALUES (?)",
            ("artifact-factory.v9",),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerSchemaUnsupported) as raised:
            ArtifactStore(dest)
        self.assertIn("LEDGER_SCHEMA_UNSUPPORTED", str(raised.exception))

    def test_v1_migration_failure_rolls_back(self) -> None:
        self._advance_a_to_reviewing_code()
        dest = Path(self._tmp.name) / "v1-rollback.sqlite3"
        _copy_into_v1(self.store.conn, dest)
        probe = sqlite3.connect(dest)
        sql_before = probe.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
        ).fetchone()[0]
        rows_before = _lane_artifact_rows(probe)
        probe.close()

        class ExplodingStore(ArtifactStore):
            def _rebuild_lane_artifacts_current_check(self) -> None:
                self.conn.execute(
                    "ALTER TABLE lane_artifacts RENAME TO lane_artifacts__v1_backup"
                )
                raise sqlite3.DatabaseError("injected migration failure")

        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "injected migration failure"
        ):
            ExplodingStore(dest)
        probe = sqlite3.connect(dest)
        try:
            self.assertEqual(_schema_version(probe), st.LEDGER_SCHEMA_VERSION_V1)
            sql_after = probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
            ).fetchone()[0]
            self.assertEqual(sql_after, sql_before)
            self.assertEqual(_lane_artifact_rows(probe), rows_before)
            names = {
                row[0]
                for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if not str(row[0]).startswith("sqlite_")
            }
            self.assertIn("lane_artifacts", names)
            self.assertNotIn("lane_artifacts__v1_backup", names)
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
