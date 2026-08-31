"""REVIEWING_CODE private-path collision invalidates tests and returns to authoring."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adw_modules import code_review as cr
from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import private_review as prv
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import (
    ArtifactStore,
    LANE_ARTIFACT_COLUMNS,
    V1_LANE_ARTIFACTS_SQL,
)
from adw_modules.runtime_state import RuntimeStateRoot

PRODUCT = "src/lib/seo/geo-entity-page.test.ts"
HIDDEN = "tests/hidden/test_geo_entity_page_meta.py"
LANE_ID = "lane-wp6-tests"
RUN_ID = "run-collision"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    nested = path / "src" / "lib" / "seo"
    nested.mkdir(parents=True)
    (nested / "geo-entity-page.test.ts").write_text("base test\n", encoding="utf-8")
    _git(path, "add", "src/lib/seo/geo-entity-page.test.ts")
    _git(path, "commit", "-m", "seed")


def _plan_bytes() -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": LANE_ID,
                "needs": [],
                "outputs": [PRODUCT],
                "spec": {
                    "goal": "emit geo entity page tests",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["geo entity page test is written"],
            }
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hidden_body() -> str:
    return (
        "from pathlib import Path\n\n"
        "def test_geo_entity_page_exists():\n"
        "    assert Path({0!r}).is_file()\n"
    ).format(PRODUCT)



def _downgrade_ledger_to_v1(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE lane_artifacts RENAME TO lane_artifacts_v1_src")
        conn.execute(V1_LANE_ARTIFACTS_SQL)
        cols = ", ".join(LANE_ARTIFACT_COLUMNS)
        conn.execute(
            f"INSERT INTO lane_artifacts ({cols}) SELECT {cols} FROM lane_artifacts_v1_src"
        )
        conn.execute("DROP TABLE lane_artifacts_v1_src")
        conn.execute("DELETE FROM ledger_meta")
        conn.execute(
            "INSERT INTO ledger_meta(schema_version) VALUES (?)",
            (st.LEDGER_SCHEMA_VERSION_V1,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class CollisionActor:
    def __init__(self, repo: Path, worktrees: Path) -> None:
        self.repo = repo
        self.worktrees = worktrees
        self.write_contexts: list[sch.LaneContext] = []
        self.review_code_calls = 0

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        self.write_contexts.append(ctx)
        if len(self.write_contexts) > 4:
            raise AssertionError(
                "write_tests loop artifacts={0}".format(
                    [sorted(c.artifacts) for c in self.write_contexts]
                )
            )
        if ctx.artifacts.get("TEST_INVALIDATION") is not None:
            return {"files": {HIDDEN: _hidden_body()}}
        return {
            "files": {
                PRODUCT: (
                    "from pathlib import Path\n\n"
                    "def test_secret_selector():\n"
                    "    assert Path({0!r}).is_file()\n"
                ).format(PRODUCT)
            }
        }

    def review_tests(self, ctx: sch.LaneContext):
        del ctx
        return st.ReviewerVerdict.PASS, ()


    def build(self, ctx: sch.LaneContext) -> dict:
        work = self.worktrees / ctx.lane.lane_id / ctx.input_digest[:12]
        if work.exists():
            _git(self.repo, "worktree", "remove", "--force", str(work))
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        for path in ctx.lane.declared_outputs:
            target = work / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "{0}:{1}:{2}\n".format(
                    ctx.lane.lane_id, ctx.plan_revision, ctx.input_digest
                ),
                encoding="utf-8",
            )
            _git(work, "add", path)
        if not _git(work, "status", "--porcelain"):
            raise AssertionError("empty production commit")
        _git(work, "commit", "-m", ctx.lane.lane_id)
        return {"candidate_sha": _git(work, "rev-parse", "HEAD"), "changed": True}



    def review_code(self, ctx: sch.LaneContext):
        del ctx
        self.review_code_calls += 1
        return st.ReviewerVerdict.PASS, ()

    def review_integration(self, ctx, lanes, integration_sha):
        del ctx, lanes, integration_sha
        return st.ReviewerVerdict.PASS, (), ()

    def publish(self, ctx, *, fingerprint, expected_before, published_sha):
        del expected_before
        return {
            "receipt_object": published_sha,
            "receipt_ref": st.publication_ref(ctx.run_id, fingerprint),
        }


class PrivatePathInvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)

    def _start_run(self, run_id: str) -> gitpub.TargetBinding:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:collision"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        return target

    def _rows(self, run_id: str, kind: st.ArtifactKind) -> list[tuple[str, dict]]:
        rows = []
        for artifact_id, payload in self.store.conn.execute(
            "SELECT artifact_id, payload_json FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind=? ORDER BY sequence",
            (run_id, LANE_ID, kind.value),
        ):
            rows.append((artifact_id, json.loads(payload)))
        return rows

    def test_reviewing_code_collision_invalidates_and_hidden_suite_progresses(
        self,
    ) -> None:
        target = self._start_run(RUN_ID)
        actor = CollisionActor(self.repo, self.runtime.path / "worktrees")
        scheduler = sch.FactoryScheduler(
            self.store, RUN_ID, actor, self.runtime, target
        )
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)

        invalidations = self._rows(RUN_ID, st.ArtifactKind.TEST_INVALIDATION)
        self.assertEqual(len(invalidations), 1)
        reason = invalidations[0][1]["reason"]
        self.assertEqual(invalidations[0][1]["code"], "PRIVATE_PATH_COLLISION")
        self.assertIn(PRODUCT, reason["observed_behavior"])
        self.assertIn("hidden validator/meta-test", reason["required_behavior"])
        self.assertEqual(self.store.lane_stage(RUN_ID, LANE_ID), st.LaneStage.MERGED)

        self.assertGreaterEqual(len(actor.write_contexts), 2)
        first_digest = actor.write_contexts[0].input_digest
        invalidated = [
            ctx
            for ctx in actor.write_contexts
            if ctx.artifacts.get("TEST_INVALIDATION") is not None
        ]
        self.assertEqual(len(invalidated), 1)
        second = invalidated[0]
        self.assertNotEqual(second.input_digest, first_digest)
        payload = second.artifacts["TEST_INVALIDATION"].payload
        self.assertEqual(payload["code"], "PRIVATE_PATH_COLLISION")
        self.assertIn(PRODUCT, payload["reason"]["observed_behavior"])

        drafts = self._rows(RUN_ID, st.ArtifactKind.TEST_DRAFT)
        self.assertGreaterEqual(len(drafts), 2)
        sealed = self._rows(RUN_ID, st.ArtifactKind.SEALED_TEST_BUNDLE)
        self.assertGreaterEqual(len(sealed), 2)
        builders = self._rows(RUN_ID, st.ArtifactKind.BUILDER_OUTPUT)
        self.assertGreaterEqual(len(builders), 2)
        reviews = self._rows(RUN_ID, st.ArtifactKind.CODE_REVIEW)
        self.assertGreaterEqual(len(reviews), 1)
        self.assertTrue(actor.review_code_calls >= 1)

    def test_resume_records_invalidation_once(self) -> None:
        run_id = "run-collision-resume"
        target = self._start_run(run_id)
        actor = CollisionActor(self.repo, self.runtime.path / "worktrees")
        real_complete = self.store.complete_stage

        def boom(
            run_id_arg,
            lane_id,
            expected_stage,
            expected_input_digest,
            artifact,
            next_stage,
        ):
            if artifact.kind is st.ArtifactKind.TEST_INVALIDATION:
                raise RuntimeError("death before invalidation")
            return real_complete(
                run_id_arg,
                lane_id,
                expected_stage,
                expected_input_digest,
                artifact,
                next_stage,
            )

        self.store.complete_stage = boom  # type: ignore[method-assign]
        scheduler = sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, target
        )
        with self.assertRaisesRegex(RuntimeError, "death before invalidation"):
            scheduler.run()
        self.assertEqual(
            self.store.lane_stage(run_id, LANE_ID), st.LaneStage.REVIEWING_CODE
        )
        self.assertEqual(self._rows(run_id, st.ArtifactKind.TEST_INVALIDATION), [])
        self.assertEqual(self._rows(run_id, st.ArtifactKind.CODE_REVIEW), [])

        self.store.complete_stage = real_complete  # type: ignore[method-assign]
        resumed = sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, target
        )
        self.assertEqual(resumed.run(), st.RunStatus.COMPLETE)
        self.assertEqual(len(self._rows(run_id, st.ArtifactKind.TEST_INVALIDATION)), 1)
        self.assertEqual(self.store.lane_stage(run_id, LANE_ID), st.LaneStage.MERGED)

    def test_resume_after_v1_migration_records_invalidation_once(self) -> None:
        run_id = "run-collision-resume-v1"
        target = self._start_run(run_id)
        actor = CollisionActor(self.repo, self.runtime.path / "worktrees")
        real_complete = self.store.complete_stage

        def boom(
            run_id_arg,
            lane_id,
            expected_stage,
            expected_input_digest,
            artifact,
            next_stage,
        ):
            if artifact.kind is st.ArtifactKind.TEST_INVALIDATION:
                raise RuntimeError("death before invalidation")
            return real_complete(
                run_id_arg,
                lane_id,
                expected_stage,
                expected_input_digest,
                artifact,
                next_stage,
            )

        self.store.complete_stage = boom  # type: ignore[method-assign]
        scheduler = sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, target
        )
        with self.assertRaisesRegex(RuntimeError, "death before invalidation"):
            scheduler.run()
        self.assertEqual(
            self.store.lane_stage(run_id, LANE_ID), st.LaneStage.REVIEWING_CODE
        )
        self.assertEqual(self._rows(run_id, st.ArtifactKind.TEST_INVALIDATION), [])
        self.assertEqual(self._rows(run_id, st.ArtifactKind.CODE_REVIEW), [])

        ledger = self.runtime.ledger_path()
        self.store.close()
        _downgrade_ledger_to_v1(ledger)
        probe = sqlite3.connect(ledger)
        try:
            version = probe.execute(
                "SELECT schema_version FROM ledger_meta"
            ).fetchone()[0]
            self.assertEqual(version, st.LEDGER_SCHEMA_VERSION_V1)
            sql = probe.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='lane_artifacts'"
            ).fetchone()[0]
            self.assertNotIn("TEST_INVALIDATION", sql)
        finally:
            probe.close()

        self.store = ArtifactStore(ledger)
        self.addCleanup(self.store.close)
        resumed = sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, target
        )
        self.assertEqual(resumed.run(), st.RunStatus.COMPLETE)
        self.assertEqual(len(self._rows(run_id, st.ArtifactKind.TEST_INVALIDATION)), 1)
        self.assertEqual(self.store.lane_stage(run_id, LANE_ID), st.LaneStage.MERGED)

    def test_arbitrary_isolation_error_fails_closed(self) -> None:
        run_id = "run-isolation-fail-closed"
        target = self._start_run(run_id)

        class HiddenOnly(CollisionActor):
            def write_tests(self, ctx: sch.LaneContext) -> dict:
                self.write_contexts.append(ctx)
                return {"files": {HIDDEN: _hidden_body()}}

            def review_code(self, ctx: sch.LaneContext):
                del ctx
                raise RuntimeError("stop at review")

        actor = HiddenOnly(self.repo, self.runtime.path / "worktrees")
        scheduler = sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, target
        )
        with self.assertRaisesRegex(RuntimeError, "stop at review"):
            scheduler.run()
        self.assertEqual(
            self.store.lane_stage(run_id, LANE_ID), st.LaneStage.REVIEWING_CODE
        )

        def isolation(*_args, **_kwargs):
            raise prv.IsolationError("builder rev-list reaches private object")

        with mock.patch.object(
            cr, "detect_candidate_private_collisions", side_effect=isolation
        ):
            closed = sch.FactoryScheduler(
                self.store, run_id, CollisionActor(self.repo, self.runtime.path / "worktrees"),
                self.runtime,
                target,
            )
            with self.assertRaises(prv.IsolationError) as raised:
                closed.run()
        self.assertNotIsInstance(raised.exception, prv.PrivatePathCollisionError)
        self.assertEqual(
            self.store.lane_stage(run_id, LANE_ID), st.LaneStage.REVIEWING_CODE
        )
        self.assertEqual(self._rows(run_id, st.ArtifactKind.TEST_INVALIDATION), [])


if __name__ == "__main__":
    unittest.main()

