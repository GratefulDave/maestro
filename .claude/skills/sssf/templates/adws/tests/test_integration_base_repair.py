"""Plan integration tip, tester checkout refresh, legacy rebase, OMP window."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast



ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.handoff_budget import OMP_CONTEXT_WINDOW_TOKENS
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from adw_modules.scheduler import LaneContext
from test_actor_delegation_capability import RecordingLauncher, _ROLE_ROUTES, _lane


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_prod_and_integration(path: Path) -> tuple[str, str]:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("prod\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "prod")
    prod = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-b", "integration")
    entity = path / "src" / "lib" / "seo" / "entity.ts"
    gateway = path / "src" / "lib" / "seo" / "gateway.ts"
    entity.parent.mkdir(parents=True)
    entity.write_text("export const entity = true;\n", encoding="utf-8")
    gateway.write_text("export const gateway = true;\n", encoding="utf-8")
    _git(path, "add", "src/lib/seo/entity.ts", "src/lib/seo/gateway.ts")
    _git(path, "commit", "-m", "integration")
    integration = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "main")
    return prod, integration


def _plan_bytes(branch: str) -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": "emit a.txt",
                    "integration": {"integration_branch": branch},
                },
                "acceptance": ["a.txt is written"],
            }
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compile(branch: str, ref: str = "plan:integration") -> st.CompiledPlan:
    return plan_compiler.compile_plan(
        _plan_bytes(branch), plan_revision=1, plan_artifact_ref=ref
    )

def _two_lane_plan_bytes(branch: str) -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": "emit a.txt",
                    "integration": {"integration_branch": branch},
                },
                "acceptance": ["a.txt is written"],
            },
            {
                "id": "lane-b",
                "needs": [],
                "outputs": ["b.txt"],
                "spec": {
                    "goal": "emit b.txt",
                    "integration": {"integration_branch": branch},
                },
                "acceptance": ["b.txt is written"],
            },
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compile_two(branch: str, ref: str = "plan:two") -> st.CompiledPlan:
    return plan_compiler.compile_plan(
        _two_lane_plan_bytes(branch), plan_revision=1, plan_artifact_ref=ref
    )


def _write_retarget_journal(
    state: Path, run_id: str, from_sha: str, to_sha: str
) -> Path:
    path = sch.legacy_retarget_journal_path(state, run_id)
    path.write_bytes(
        st.canonical_bytes(
            {"from_sha": from_sha, "run_id": run_id, "to_sha": to_sha}
        )
    )
    return path



class PlanIntegrationPinTest(unittest.TestCase):
    def test_new_run_pins_integration_ref_not_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/integration")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            self.assertEqual(target.target_initial_main_sha, prod)
            self.assertEqual(target.integration_initial_sha, prod)
            binding = sch.create_factory_run(
                store=store,
                run_id="run-pin",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            self.assertEqual(binding.target_initial_main_sha, prod)
            self.assertEqual(binding.integration_initial_sha, integration)
            self.assertEqual(binding.target_main_ref, "refs/heads/main")
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-pin")),
                integration,
            )
            self.assertEqual(_git(product, "rev-parse", "refs/heads/main"), prod)

    def test_inconsistent_lane_refs_are_refused(self) -> None:
        document = {
            "schema_version": "maestro-plan.artifact-factory.v1",
            "lanes": [
                {
                    "id": "lane-a",
                    "needs": [],
                    "outputs": ["a.txt"],
                    "spec": {
                        "goal": "a",
                        "integration": {"integration_branch": "refs/heads/main"},
                    },
                    "acceptance": ["a.txt is written"],
                },
                {
                    "id": "lane-b",
                    "needs": ["lane-a"],
                    "outputs": ["b.txt"],
                    "spec": {
                        "goal": "b",
                        "integration": {
                            "integration_branch": "refs/heads/integration"
                        },
                    },
                    "acceptance": ["b.txt is written"],
                },
            ],
        }
        compiled = plan_compiler.compile_plan(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            plan_revision=1,
            plan_artifact_ref="plan:bad",
        )
        with self.assertRaises(gitpub.GitPublicationRefused) as raised:
            gitpub.declared_integration_ref(gitpub.lane_specs_from_plan(compiled))
        self.assertEqual(raised.exception.code, "INCONSISTENT_INTEGRATION_REF")


class TesterCheckoutRefreshTest(unittest.TestCase):
    def test_tester_checkout_is_integration_and_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            target = gitpub.pin_integration_sha(
                gitpub.bind_target_worktree(product, "refs/heads/main"),
                "refs/heads/integration",
            )
            self.assertEqual(target.integration_initial_sha, integration)
            self.assertEqual(target.target_initial_main_sha, prod)
            ctx = LaneContext(
                run_id="run-checkout",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="ef" * 32,
                stage=st.LaneStage.WRITING_TESTS,
                artifacts={},
                integration_head=integration,
            )
            revise = LaneContext(
                run_id="run-checkout",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="aa" * 32,
                stage=st.LaneStage.WRITING_TESTS,
                artifacts={},
                integration_head=integration,
            )
            recorder = RecordingLauncher(files={"tests/hidden.py": "assert True\n"})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            actor.write_tests(ctx)
            checkout = Path(recorder.launches[0]["worktree"])
            self.assertEqual(recorder.launches[0]["head"], integration)
            self.assertTrue((checkout / "src/lib/seo/entity.ts").is_file())
            self.assertTrue((checkout / "src/lib/seo/gateway.ts").is_file())
            self.assertFalse((product / "src/lib/seo/entity.ts").exists())
            self.assertNotEqual(checkout.resolve(), product.resolve())
            self.assertTrue(
                (state / "worktrees").resolve() in checkout.resolve().parents
            )
            self.assertEqual(checkout.parent.name, "tester")
            (checkout / "stale.txt").write_text("dirty\n", encoding="utf-8")
            actor.write_tests(revise)
            self.assertEqual(recorder.resubmits[0]["head"], integration)
            self.assertFalse((checkout / "stale.txt").exists())
            self.assertTrue((checkout / "src/lib/seo/entity.ts").is_file())
            builder = state / "worktrees" / "run-checkout" / "lane-a" / "builder"
            builder_checkout = builder / "checkout"
            self.assertFalse((builder_checkout / ".git").exists())
            self.assertFalse((builder_checkout / "src/lib/seo/entity.ts").exists())
            self.assertFalse((builder_checkout / "tests" / "hidden.py").exists())

    def test_cold_actor_refreshes_existing_checkout_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            target = gitpub.pin_integration_sha(
                gitpub.bind_target_worktree(product, "refs/heads/main"),
                "refs/heads/integration",
            )
            ctx = LaneContext(
                run_id="run-cold",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="ef" * 32,
                stage=st.LaneStage.WRITING_TESTS,
                artifacts={},
                integration_head=integration,
            )
            recorder = RecordingLauncher(files={"tests/hidden.py": "assert True\n"})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            actor.write_tests(ctx)
            checkout = Path(recorder.launches[0]["worktree"])
            _git(checkout, "reset", "--hard", prod)
            (checkout / "stale.txt").write_text("dirty\n", encoding="utf-8")
            self.assertFalse((checkout / "src/lib/seo/entity.ts").exists())
            recorder._live.clear()
            cold = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            cold.write_tests(ctx)
            self.assertEqual(len(recorder.launches), 2)
            self.assertEqual(len(recorder.resubmits), 0)
            self.assertEqual(recorder.launches[1]["head"], integration)
            self.assertFalse((checkout / "stale.txt").exists())
            self.assertTrue((checkout / "src/lib/seo/entity.ts").is_file())
            self.assertFalse(recorder.launches[1]["hidden_test_at_launch"])




class BuilderCheckoutRefreshTest(unittest.TestCase):
    def test_absent_new_declared_output_does_not_break_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            target = gitpub.pin_integration_sha(
                gitpub.bind_target_worktree(product, "refs/heads/main"),
                "refs/heads/integration",
            )
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, RecordingLauncher()), state, target, _ROLE_ROUTES
            )
            checkout = state / "worktrees" / "run-refresh" / "lane-a" / "builder" / "checkout"
            actor._add_worktree(checkout, prod)
            (checkout / "old-candidate.txt").write_text("old\n", encoding="utf-8")
            actor._git(checkout, "add", "old-candidate.txt")
            actor._git(checkout, "commit", "-m", "old candidate")

            actor._refresh_builder_checkout(
                checkout, ("src/lib/seo/faq.ts",), integration
            )

            self.assertEqual(_git(checkout, "rev-parse", "HEAD"), integration)
            self.assertTrue((checkout / "src/lib/seo/entity.ts").is_file())
            self.assertFalse((checkout / "old-candidate.txt").exists())
            self.assertFalse((checkout / "src/lib/seo/faq.ts").exists())


class WritingTestsDigestIdentityTest(unittest.TestCase):
    def test_base_sha_change_prevents_stale_digest(self) -> None:
        first = st.writing_tests_input_digest(
            run_id="run-1",
            lane_id="lane-a",
            plan_revision=1,
            plan_digest="aa" * 32,
            spec_digest="bb" * 32,
            projection_digest="cc" * 32,
            lane_plan_id="dd" * 32,
            test_review_id=st.NO_TEST_REVIEW,
            integration_head="11" * 20,
        )
        second = st.writing_tests_input_digest(
            run_id="run-1",
            lane_id="lane-a",
            plan_revision=1,
            plan_digest="aa" * 32,
            spec_digest="bb" * 32,
            projection_digest="cc" * 32,
            lane_plan_id="dd" * 32,
            test_review_id=st.NO_TEST_REVIEW,
            integration_head="22" * 20,
        )
        self.assertNotEqual(first, second)


class LegacyIntegrationCorrectionTest(unittest.TestCase):
    def test_defer_while_reviewing_tests(self) -> None:
        self.assertEqual(
            sch.legacy_integration_correction_decision(
                stored_sha="11" * 20,
                declared_sha="22" * 20,
                lane_stages=(st.LaneStage.REVIEWING_TESTS,),
                artifact_kinds=(st.ArtifactKind.LANE_PLAN, st.ArtifactKind.TEST_DRAFT),
            ),
            "defer",
        )

    def test_apply_defers_while_reviewing_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/main")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id="run-review",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            store.conn.execute(
                "UPDATE lane_state SET stage=? WHERE run_id=?",
                (st.LaneStage.REVIEWING_TESTS.value, "run-review"),
            )
            store.conn.commit()
            updated = sch.correct_legacy_integration_base(
                store=store,
                target=target,
                run_id="run-review",
                declared_sha=integration,
            )
            row = sch.run_row(store, "run-review")
            self.assertEqual(row["integration_initial_sha"], prod)
            self.assertEqual(updated.integration_initial_sha, prod)
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-review")),
                prod,
            )

    def test_refuse_after_builder_or_merge(self) -> None:
        for kind in (
            st.ArtifactKind.BUILDER_OUTPUT,
            st.ArtifactKind.INTEGRATION_MERGE,
            st.ArtifactKind.MAIN_PUBLICATION,
        ):
            self.assertEqual(
                sch.legacy_integration_correction_decision(
                    stored_sha="11" * 20,
                    declared_sha="22" * 20,
                    lane_stages=(st.LaneStage.WRITING_TESTS,),
                    artifact_kinds=(st.ArtifactKind.LANE_PLAN, kind),
                ),
                "refuse",
            )

    def test_migrate_unbuilt_unmerged_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/main")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id="run-legacy",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            row = sch.run_row(store, "run-legacy")
            self.assertEqual(row["integration_initial_sha"], prod)
            updated = sch.correct_legacy_integration_base(
                store=store,
                target=target,
                run_id="run-legacy",
                declared_sha=integration,
            )
            row = sch.run_row(store, "run-legacy")
            self.assertEqual(row["integration_initial_sha"], integration)
            self.assertEqual(updated.integration_initial_sha, integration)
            self.assertEqual(updated.target_initial_main_sha, prod)
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-legacy")),
                integration,
            )
            reasons = [
                item[0]
                for item in store.conn.execute(
                    "SELECT reason FROM transitions WHERE run_id=? ORDER BY id",
                    ("run-legacy",),
                )
            ]
            self.assertIn("legacy_integration_retarget", reasons)

    def test_apply_refuses_after_builder_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/main")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id="run-built",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            store.conn.execute(
                "INSERT INTO lane_artifacts("
                "artifact_id, run_id, lane_id, sequence, completed_stage, "
                "artifact_kind, plan_revision, spec_digest, lane_projection_digest, "
                "input_digest, output_digest, artifact_ref, payload_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ee" * 32,
                    "run-built",
                    "lane-a",
                    1,
                    st.LaneStage.BUILDING.value,
                    st.ArtifactKind.BUILDER_OUTPUT.value,
                    1,
                    compiled.lanes[0].spec_digest,
                    compiled.lanes[0].lane_projection_digest,
                    "ff" * 32,
                    "00" * 32,
                    "builder:x",
                    "{}",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            store.conn.commit()
            with self.assertRaises(sch.FactoryRefused) as raised:
                sch.correct_legacy_integration_base(
                    store=store,
                    target=target,
                    run_id="run-built",
                    declared_sha=integration,
                )
            self.assertIn("LEGACY_INTEGRATION_REBASE_UNSAFE", str(raised.exception))
            row = sch.run_row(store, "run-built")
            self.assertEqual(row["integration_initial_sha"], prod)

    def test_git_ahead_of_db_reconciles_then_ensure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/main")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id="run-split-git",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            _write_retarget_journal(state, "run-split-git", prod, integration)
            gitpub.retarget_integration_ref(
                target, "run-split-git", prod, integration
            )
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-split-git")),
                integration,
            )
            self.assertEqual(
                sch.run_row(store, "run-split-git")["integration_initial_sha"], prod
            )
            with self.assertRaises(gitpub.GitPublicationRefused) as raised:
                sch.ensure_run_integration_ref(target, store, "run-split-git")
            self.assertEqual(raised.exception.code, "INTEGRATION_REF_COLLISION")
            updated = sch.correct_legacy_integration_base(
                store=store,
                target=target,
                run_id="run-split-git",
                declared_sha=integration,
            )
            self.assertEqual(updated.integration_initial_sha, integration)
            self.assertEqual(
                sch.run_row(store, "run-split-git")["integration_initial_sha"],
                integration,
            )
            self.assertEqual(
                sch.ensure_run_integration_ref(updated, store, "run-split-git"),
                integration,
            )
            self.assertFalse(
                sch.legacy_retarget_journal_path(state, "run-split-git").exists()
            )

    def test_db_ahead_of_git_reconciles_then_ensure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/main")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id="run-split-db",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            _write_retarget_journal(state, "run-split-db", prod, integration)
            store.retarget_integration_initial_sha("run-split-db", prod, integration)
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-split-db")),
                prod,
            )
            self.assertEqual(
                sch.run_row(store, "run-split-db")["integration_initial_sha"],
                integration,
            )
            with self.assertRaises(gitpub.GitPublicationRefused) as raised:
                sch.ensure_run_integration_ref(target, store, "run-split-db")
            self.assertEqual(raised.exception.code, "INTEGRATION_REF_COLLISION")
            updated = sch.correct_legacy_integration_base(
                store=store,
                target=target,
                run_id="run-split-db",
                declared_sha=integration,
            )
            self.assertEqual(updated.integration_initial_sha, integration)
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-split-db")),
                integration,
            )
            self.assertEqual(
                sch.ensure_run_integration_ref(updated, store, "run-split-db"),
                integration,
            )
            self.assertFalse(
                sch.legacy_retarget_journal_path(state, "run-split-db").exists()
            )

    def test_death_after_git_retries_to_consistent_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/main")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id="run-death-git",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            original = gitpub.retarget_integration_ref

            def boom(*args: Any, **kwargs: Any) -> Any:
                result = original(*args, **kwargs)
                raise RuntimeError("death after git")

            gitpub.retarget_integration_ref = boom  # type: ignore[method-assign]
            try:
                with self.assertRaises(RuntimeError):
                    sch.correct_legacy_integration_base(
                        store=store,
                        target=target,
                        run_id="run-death-git",
                        declared_sha=integration,
                    )
            finally:
                gitpub.retarget_integration_ref = original  # type: ignore[method-assign]
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-death-git")),
                integration,
            )
            self.assertEqual(
                sch.run_row(store, "run-death-git")["integration_initial_sha"], prod
            )
            self.assertTrue(
                sch.legacy_retarget_journal_path(state, "run-death-git").is_file()
            )
            updated = sch.correct_legacy_integration_base(
                store=store,
                target=target,
                run_id="run-death-git",
                declared_sha=integration,
            )
            self.assertEqual(
                sch.ensure_run_integration_ref(updated, store, "run-death-git"),
                integration,
            )
            self.assertFalse(
                sch.legacy_retarget_journal_path(state, "run-death-git").exists()
            )



class _StopStartup(Exception):
    pass


class _StartupActor:
    def write_tests(self, ctx: sch.LaneContext) -> dict[str, Any]:
        del ctx
        raise _StopStartup()

    def review_tests(self, ctx: sch.LaneContext) -> tuple[Any, tuple[Any, ...]]:
        del ctx
        raise AssertionError("review_tests")

    def build(self, ctx: sch.LaneContext) -> dict[str, Any]:
        del ctx
        raise AssertionError("build")

    def review_code(self, ctx: sch.LaneContext) -> tuple[Any, tuple[Any, ...]]:
        del ctx
        raise AssertionError("review_code")

    def review_integration(self, ctx: Any, lanes: Any, integration_sha: str) -> Any:
        del ctx, lanes, integration_sha
        raise AssertionError("review_integration")

    def publish(
        self,
        ctx: Any,
        *,
        fingerprint: str,
        expected_before: str,
        published_sha: str,
    ) -> dict[str, Any]:
        del ctx, fingerprint, expected_before, published_sha
        raise AssertionError("publish")


class FactorySchedulerStartupRecoveryTest(unittest.TestCase):
    def _boot(self, run_id: str) -> tuple[Path, Path, str, str, RuntimeStateRoot, ArtifactStore, gitpub.TargetBinding]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        product = root / "product"
        state = root / "state"
        state.mkdir(mode=0o700)
        prod, integration = _init_prod_and_integration(product)
        runtime = RuntimeStateRoot(state, overlap_paths=(product,))
        runtime.ensure_layout()
        store = ArtifactStore(runtime.ledger_path())
        self.addCleanup(store.close)
        self.addCleanup(runtime.close)
        compiled = _compile("refs/heads/main")
        target = gitpub.bind_target_worktree(product, "refs/heads/main")
        sch.create_factory_run(
            store=store,
            run_id=run_id,
            compiled=compiled,
            runtime=runtime,
            target=target,
        )
        return product, state, prod, integration, runtime, store, target

    def test_run_recovers_git_ahead_of_db_journal_before_orphan(self) -> None:
        product, state, prod, integration, runtime, store, target = self._boot(
            "run-start-git"
        )
        _write_retarget_journal(state, "run-start-git", prod, integration)
        gitpub.retarget_integration_ref(target, "run-start-git", prod, integration)
        scheduler = sch.FactoryScheduler(
            store, "run-start-git", _StartupActor(), runtime, target
        )
        with self.assertRaises(_StopStartup):
            scheduler.run()
        self.assertEqual(
            sch.run_row(store, "run-start-git")["integration_initial_sha"],
            integration,
        )
        self.assertEqual(scheduler.target.integration_initial_sha, integration)
        self.assertEqual(
            _git(product, "rev-parse", st.integration_ref("run-start-git")),
            integration,
        )
        self.assertEqual(
            sch.ensure_run_integration_ref(scheduler.target, store, "run-start-git"),
            integration,
        )
        self.assertFalse(
            sch.legacy_retarget_journal_path(state, "run-start-git").exists()
        )

    def test_run_recovers_db_ahead_of_git_journal_before_orphan(self) -> None:
        product, state, prod, integration, runtime, store, target = self._boot(
            "run-start-db"
        )
        _write_retarget_journal(state, "run-start-db", prod, integration)
        store.retarget_integration_initial_sha("run-start-db", prod, integration)
        scheduler = sch.FactoryScheduler(
            store, "run-start-db", _StartupActor(), runtime, target
        )
        with self.assertRaises(_StopStartup):
            scheduler.run()
        self.assertEqual(
            sch.run_row(store, "run-start-db")["integration_initial_sha"],
            integration,
        )
        self.assertEqual(scheduler.target.integration_initial_sha, integration)
        self.assertEqual(
            _git(product, "rev-parse", st.integration_ref("run-start-db")),
            integration,
        )
        self.assertEqual(
            sch.ensure_run_integration_ref(scheduler.target, store, "run-start-db"),
            integration,
        )
        self.assertFalse(
            sch.legacy_retarget_journal_path(state, "run-start-db").exists()
        )

    def test_run_orphaned_merge_without_journal_is_not_intercepted(self) -> None:
        product, state, prod, integration, runtime, store, target = self._boot(
            "run-orphan"
        )
        compiled = _compile("refs/heads/main")
        store.conn.execute(
            "INSERT INTO lane_artifacts("
            "artifact_id, run_id, lane_id, sequence, completed_stage, "
            "artifact_kind, plan_revision, spec_digest, lane_projection_digest, "
            "input_digest, output_digest, artifact_ref, payload_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ee" * 32,
                "run-orphan",
                "lane-a",
                1,
                st.LaneStage.BUILDING.value,
                st.ArtifactKind.BUILDER_OUTPUT.value,
                1,
                compiled.lanes[0].spec_digest,
                compiled.lanes[0].lane_projection_digest,
                "ff" * 32,
                "00" * 32,
                "builder:x",
                "{}",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        store.conn.commit()
        gitpub.retarget_integration_ref(target, "run-orphan", prod, integration)
        self.assertFalse(
            sch.legacy_retarget_journal_path(state, "run-orphan").exists()
        )

        class SpecsActor(_StartupActor):
            lane_specs = {
                "lane-a": {
                    "goal": "emit a.txt",
                    "integration": {"integration_branch": "refs/heads/integration"},
                }
            }

        scheduler = sch.FactoryScheduler(
            store, "run-orphan", SpecsActor(), runtime, target
        )
        with self.assertRaises(sch.FactoryRefused) as raised:
            scheduler.run()
        self.assertIn(
            "orphaned integration merge is not uniquely attributable",
            str(raised.exception),
        )
        self.assertNotIn("LEGACY_INTEGRATION_REBASE_UNSAFE", str(raised.exception))
        self.assertEqual(
            store.lane_stage("run-orphan", "lane-a"), st.LaneStage.PLANNED
        )



class MergeEpochSecondsTest(unittest.TestCase):
    def test_naive_and_aware_stamps_match(self) -> None:
        aware = "2026-01-01T00:00:00+00:00"
        naive = "2026-01-01T00:00:00"
        self.assertEqual(sch.merge_epoch_seconds(aware), 1767225600)
        self.assertEqual(sch.merge_epoch_seconds(naive), sch.merge_epoch_seconds(aware))


class MergeOrderReconstructionTest(unittest.TestCase):
    def test_writing_tests_follows_transition_id_not_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.sqlite3"
            store = ArtifactStore(db)
            self.addCleanup(store.close)
            initial = "11" * 20
            mid = "22" * 20
            tip = "33" * 20
            lane_a = st.LaneProjection(
                lane_id="lane-a",
                needs=(),
                spec_digest="aa" * 32,
                declared_outputs=("a.txt",),
                lane_projection_digest=st.lane_projection_digest(
                    "aa" * 32, (), ("a.txt",)
                ),
                public_acceptance=("a.txt is written",),
            )
            lane_b = st.LaneProjection(
                lane_id="lane-b",
                needs=(),
                spec_digest="bb" * 32,
                declared_outputs=("b.txt",),
                lane_projection_digest=st.lane_projection_digest(
                    "bb" * 32, (), ("b.txt",)
                ),
                public_acceptance=("b.txt is written",),
            )
            plan_bytes = st.canonical_bytes({"lanes": ["lane-a", "lane-b"]})
            compiled = st.CompiledPlan(
                plan_bytes=plan_bytes,
                plan_artifact_ref="plan:order",
                plan_digest=st.digest_bytes(plan_bytes),
                plan_revision=1,
                lanes=(lane_a, lane_b),
                integration_order=("lane-a", "lane-b"),
            )
            binding = st.RunBinding(
                runtime_state_root=str(Path(tmp) / "state"),
                runtime_state_fingerprint="cc" * 32,
                integration_ref=st.integration_ref("run-order"),
                integration_initial_sha=initial,
                target_repository_root=str(Path(tmp) / "product"),
                target_git_common_dir=str(Path(tmp) / "product/.git"),
                target_worktree_git_dir=str(Path(tmp) / "product/.git"),
                target_object_format="sha1",
                target_repository_fingerprint="dd" * 32,
                target_sync_journal_fingerprint="ee" * 32,
                target_initial_main_sha=initial,
                target_main_ref="refs/heads/main",
            )
            store.create_run("run-order", compiled, binding)
            for lane in (lane_a, lane_b):
                digest = st.planned_input_digest(
                    run_id="run-order",
                    lane_id=lane.lane_id,
                    plan_revision=1,
                    plan_digest=compiled.plan_digest,
                    spec_digest=lane.spec_digest,
                    projection_digest=lane.lane_projection_digest,
                    plan_artifact_ref="plan:order",
                    needs=lane.needs,
                    declared_outputs=lane.declared_outputs,
                )
                payload = {
                    "declared_outputs": list(lane.declared_outputs),
                    "input_artifact_ids": [],
                    "input_digest": digest,
                    "needs": list(lane.needs),
                    "plan_artifact_ref": "plan:order",
                }

                store.complete_stage(
                    "run-order",
                    lane.lane_id,
                    st.LaneStage.PLANNED,
                    digest,
                    st.LaneArtifact(
                        kind=st.ArtifactKind.LANE_PLAN,
                        plan_revision=1,
                        spec_digest=lane.spec_digest,
                        lane_projection_digest=lane.lane_projection_digest,
                        input_digest=digest,
                        output_digest=st.digest_canonical(payload),
                        artifact_ref=f"lane-plan:{lane.lane_id}",
                        payload=payload,
                    ),
                    st.LaneStage.WRITING_TESTS,
                )
            def insert_merge(
                lane: st.LaneProjection,
                sequence: int,
                artifact_id: str,
                before: str,
                after: str,
            ) -> None:
                payload = json.dumps(
                    {"before_sha": before, "after_sha": after},
                    sort_keys=True,
                )
                store.conn.execute(
                    "INSERT INTO lane_artifacts("
                    "artifact_id, run_id, lane_id, sequence, completed_stage, "
                    "artifact_kind, plan_revision, spec_digest, "
                    "lane_projection_digest, input_digest, output_digest, "
                    "artifact_ref, payload_json, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        "run-order",
                        lane.lane_id,
                        sequence,
                        st.LaneStage.READY_TO_MERGE.value,
                        st.ArtifactKind.INTEGRATION_MERGE.value,
                        1,
                        lane.spec_digest,
                        lane.lane_projection_digest,
                        "ff" * 32,
                        "00" * 32,
                        f"merge:{lane.lane_id}",
                        payload,
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                store.conn.execute(
                    "INSERT INTO transitions("
                    "run_id, lane_id, from_stage, to_stage, artifact_id, "
                    "reason, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        "run-order",
                        lane.lane_id,
                        st.LaneStage.READY_TO_MERGE.value,
                        st.LaneStage.MERGED.value,
                        artifact_id,
                        "complete_stage",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            insert_merge(lane_a, 3, "a1" * 32, initial, mid)
            insert_merge(lane_b, 2, "b1" * 32, mid, tip)
            store.conn.commit()
            by_sequence = [
                json.loads(row[0])["after_sha"]
                for row in store.conn.execute(
                    "SELECT payload_json FROM lane_artifacts "
                    "WHERE run_id=? AND artifact_kind=? ORDER BY sequence",
                    ("run-order", st.ArtifactKind.INTEGRATION_MERGE.value),
                )
            ]
            payloads = store.integration_merge_payloads("run-order")
            self.assertEqual(by_sequence, [tip, mid])
            self.assertEqual([item["after_sha"] for item in payloads], [mid, tip])
            self.assertEqual(
                gitpub.durable_integration_tip(initial, payloads), tip
            )
            run = store.conn.execute(
                "SELECT * FROM runs WHERE run_id=?", ("run-order",)
            ).fetchone()
            projection = store.conn.execute(
                "SELECT * FROM dag_lanes WHERE run_id=? AND lane_id=?",
                ("run-order", "lane-a"),
            ).fetchone()
            digest = store._reconstruct_stage_digest(
                run, projection, st.LaneStage.WRITING_TESTS
            )
            plan = store._latest_lane_artifact(
                "run-order", "lane-a", st.ArtifactKind.LANE_PLAN
            )
            self.assertIsNotNone(plan)
            expected = st.writing_tests_input_digest(
                run_id="run-order",
                lane_id="lane-a",
                plan_revision=1,
                plan_digest=compiled.plan_digest,
                spec_digest=lane_a.spec_digest,
                projection_digest=lane_a.lane_projection_digest,
                lane_plan_id=plan["artifact_id"],
                test_review_id=st.NO_TEST_REVIEW,
                integration_head=tip,
            )
            self.assertEqual(digest, expected)


class WriterDefersForSiblingReviewTest(unittest.TestCase):
    def test_writer_stays_put_until_sibling_review_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile_two("refs/heads/main")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id="run-defer",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            writes: list[tuple[str, str]] = []
            reviews: list[tuple[str, str, str]] = []

            class StopBuild(Exception):
                pass

            class ObservingActor:
                lane_specs = {
                    "lane-a": {
                        "goal": "emit a.txt",
                        "integration": {
                            "integration_branch": "refs/heads/integration"
                        },
                    },
                    "lane-b": {
                        "goal": "emit b.txt",
                        "integration": {
                            "integration_branch": "refs/heads/integration"
                        },
                    },
                }

                def write_tests(self, ctx: sch.LaneContext) -> dict[str, str]:
                    writes.append((ctx.lane.lane_id, ctx.integration_head))
                    name = ctx.lane.lane_id.replace("-", "_")
                    body = (
                        "from pathlib import Path\n\n"
                        "def test_{0}_exists():\n"
                        "    assert Path({1!r}).is_file()\n"
                    ).format(name, ctx.lane.declared_outputs[0])
                    return {
                        "files": {"tests/test_{0}_private.py".format(name): body}
                    }

                def review_tests(self, ctx: sch.LaneContext):
                    b_row = store.conn.execute(
                        "SELECT * FROM dag_lanes WHERE run_id=? AND lane_id=?",
                        ("run-defer", "lane-b"),
                    ).fetchone()
                    run_row = store.conn.execute(
                        "SELECT * FROM runs WHERE run_id=?", ("run-defer",)
                    ).fetchone()
                    reviews.append(
                        (
                            ctx.lane.lane_id,
                            store.lane_stage("run-defer", "lane-b").value,
                            sch.run_row(store, "run-defer")[
                                "integration_initial_sha"
                            ],
                            store._reconstruct_stage_digest(
                                run_row, b_row, st.LaneStage.WRITING_TESTS
                            ),
                        )
                    )
                    return st.ReviewerVerdict.PASS, ()

                def build(self, ctx: sch.LaneContext) -> dict[str, Any]:
                    del ctx
                    raise StopBuild()

                def review_code(self, ctx: sch.LaneContext):
                    raise AssertionError("review_code")

                def review_integration(self, ctx, lanes, integration_sha):
                    raise AssertionError("review_integration")

                def publish(self, ctx, *, fingerprint, expected_before, published_sha):
                    raise AssertionError("publish")

            actor = ObservingActor()
            scheduler = sch.FactoryScheduler(
                store, "run-defer", actor, runtime, target
            )
            scheduler._planned("lane-a")
            scheduler._planned("lane-b")
            scheduler._writing_tests("lane-a")
            self.assertEqual(store.lane_stage("run-defer", "lane-a"), st.LaneStage.REVIEWING_TESTS)
            self.assertEqual(store.lane_stage("run-defer", "lane-b"), st.LaneStage.WRITING_TESTS)
            self.assertEqual(writes, [("lane-a", prod)])
            b_digest_before = store._reconstruct_stage_digest(
                store.conn.execute(
                    "SELECT * FROM runs WHERE run_id=?", ("run-defer",)
                ).fetchone(),
                store.conn.execute(
                    "SELECT * FROM dag_lanes WHERE run_id=? AND lane_id=?",
                    ("run-defer", "lane-b"),
                ).fetchone(),
                st.LaneStage.WRITING_TESTS,
            )
            with self.assertRaises(StopBuild):
                scheduler.run()
            self.assertEqual(reviews[0][0], "lane-a")
            self.assertEqual(reviews[0][1], st.LaneStage.WRITING_TESTS.value)
            self.assertEqual(reviews[0][2], prod)
            self.assertEqual(reviews[0][3], b_digest_before)
            self.assertNotEqual(
                store.lane_stage("run-defer", "lane-b"),
                st.LaneStage.WRITING_TESTS,
            )

            self.assertIn(("lane-b", integration), writes)
            self.assertEqual(
                sch.run_row(store, "run-defer")["integration_initial_sha"],
                integration,
            )
            self.assertNotEqual(writes[0][1], writes[-1][1])



class PinOnceWhileBranchAdvancesTest(unittest.TestCase):
    def test_create_factory_run_resolves_integration_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/integration")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            pins: list[str] = []
            original = gitpub.pin_integration_sha

            def wrapped(
                binding: gitpub.TargetBinding, ref: str
            ) -> gitpub.TargetBinding:
                result = original(binding, ref)
                pins.append(result.integration_initial_sha)
                _git(product, "checkout", "integration")
                extra = product / "moved.txt"
                extra.write_text("moved\n", encoding="utf-8")
                _git(product, "add", "moved.txt")
                _git(product, "commit", "-m", "advance")
                _git(product, "checkout", "main")
                return result

            gitpub.pin_integration_sha = wrapped  # type: ignore[method-assign]
            try:
                binding = sch.create_factory_run(
                    store=store,
                    run_id="run-once",
                    compiled=compiled,
                    runtime=runtime,
                    target=target,
                )
            finally:
                gitpub.pin_integration_sha = original  # type: ignore[method-assign]
            advanced = _git(product, "rev-parse", "refs/heads/integration")
            self.assertEqual(len(pins), 1)
            self.assertEqual(pins[0], integration)
            self.assertNotEqual(advanced, integration)
            self.assertEqual(binding.integration_initial_sha, integration)
            self.assertEqual(
                sch.run_row(store, "run-once")["integration_initial_sha"], integration
            )
            restored = sch.target_from_binding(binding)
            self.assertEqual(restored.integration_initial_sha, integration)
            self.assertEqual(restored.target_initial_main_sha, prod)
            self.assertEqual(
                _git(product, "rev-parse", st.integration_ref("run-once")),
                integration,
            )

    def test_target_from_binding_keeps_stored_main_sha_when_main_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            prod, integration = _init_prod_and_integration(product)
            runtime = RuntimeStateRoot(state, overlap_paths=(product,))
            runtime.ensure_layout()
            store = ArtifactStore(runtime.ledger_path())
            self.addCleanup(store.close)
            self.addCleanup(runtime.close)
            compiled = _compile("refs/heads/integration")
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            binding = sch.create_factory_run(
                store=store,
                run_id="run-main-move",
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            later = product / "later.txt"
            later.write_text("later\n", encoding="utf-8")
            _git(product, "add", "later.txt")
            _git(product, "commit", "-m", "move main")
            moved = _git(product, "rev-parse", "refs/heads/main")
            self.assertNotEqual(moved, prod)
            live = gitpub.bind_target_worktree(product, "refs/heads/main")
            self.assertEqual(live.target_initial_main_sha, moved)
            restored = sch.target_from_binding(binding)
            self.assertEqual(restored.target_initial_main_sha, prod)
            self.assertEqual(restored.integration_initial_sha, integration)



class OmpContextWindowTest(unittest.TestCase):
    def test_15493_byte_prompt_passes_262144_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session"
            session.mkdir()
            prompt = root / "prompt.json"
            prompt.write_bytes(b"x" * 15493)
            spec = lch.LaunchSpec(
                correlation_token="run:lane:role",
                worktree=root,
                prompt_path=prompt,
                envelope_path=lch.role_result_path(root, 1),
                route="omp",
                model="grok",
                effort="high",
                profile="grok-build",
                session_dir=session,
                context_window_tokens=OMP_CONTEXT_WINDOW_TOKENS,
            )
            estimate = lch.preflight_launch_prompt(spec)
            self.assertIsNotNone(estimate)
            self.assertEqual(OMP_CONTEXT_WINDOW_TOKENS, 262144)
            tight = lch.LaunchSpec(
                correlation_token="run:lane:role",
                worktree=root,
                prompt_path=prompt,
                envelope_path=lch.role_result_path(root, 1),
                route="omp",
                model="grok",
                effort="high",
                profile="grok-build",
                session_dir=session,
                context_window_tokens=8192,
            )
            with self.assertRaises(lch.LaunchRefused) as raised:
                lch.preflight_launch_prompt(tight)
            self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.PROMPT_TOO_LARGE)


if __name__ == "__main__":
    unittest.main()
