"""Observable two-lane factory cutover: stages, sealing, resume, CLI verbs."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import hidden_vault as hv
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import AmendmentRefused, ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot

FINDING = {
    "implementation_area": "tests",
    "observed_behavior": "no cases",
    "required_behavior": "behavior is asserted",
    "violated_requirement": "public acceptance",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")


def _plan_bytes(*, a_goal: str = "emit a.txt") -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": a_goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["a.txt is written"],
            },
            {
                "id": "lane-b",
                "needs": ["lane-a"],
                "outputs": ["b.txt"],
                "spec": {
                    "goal": "emit b.txt after a",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["b.txt is written"],
            },
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ScriptedActor:
    def __init__(self, repo: Path, worktrees: Path) -> None:
        self.repo = repo
        self.worktrees = worktrees
        self.test_rounds: dict[str, int] = defaultdict(int)
        self.code_rounds: dict[str, int] = defaultdict(int)
        self.builder_payloads: list[dict] = []
        self.building_entries: list[tuple[str, st.BuildingEntryKind, int]] = []

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        name = ctx.lane.lane_id.replace("-", "_")
        lines = ["# secret-selector", "from pathlib import Path", ""]
        for output in ctx.lane.declared_outputs:
            ident = Path(output).stem.replace("-", "_")
            lines.append("def test_{0}_{1}_exists():".format(name, ident))
            lines.append("    assert Path({0!r}).is_file()".format(output))
            lines.append("")
        return {
            "files": {"tests/test_{0}_private.py".format(name): "\n".join(lines)},
            "private_tokens": ("secret-selector",),
        }

    def review_tests(self, ctx: sch.LaneContext):
        n = self.test_rounds[ctx.lane.lane_id]
        self.test_rounds[ctx.lane.lane_id] += 1
        if n == 0:
            return st.ReviewerVerdict.REVISE, (FINDING,)
        return st.ReviewerVerdict.PASS, ()

    def build(self, ctx: sch.LaneContext) -> dict:
        self.building_entries.append(
            (ctx.lane.lane_id, ctx.entry_kind, ctx.plan_revision)
        )
        work = self.worktrees / ctx.lane.lane_id / ctx.input_digest[:12]
        if work.exists():
            _git(self.repo, "worktree", "remove", "--force", str(work))
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        for path in ctx.lane.declared_outputs:
            (work / path).write_text(
                "{0}:{1}:{2}\n".format(
                    ctx.lane.lane_id, ctx.plan_revision, ctx.input_digest
                ),
                encoding="utf-8",
            )
            _git(work, "add", path)
        if not _git(work, "status", "--porcelain"):
            raise AssertionError("empty production commit")
        _git(work, "commit", "-m", ctx.lane.lane_id)
        sha = _git(work, "rev-parse", "HEAD")
        payload = {
            "candidate_sha": sha,
            "changed": True,
            "private_tokens": ("secret-selector",),
        }
        self.builder_payloads.append(dict(payload))
        if "secret-selector" in json.dumps(ctx.public_contract or {}):
            raise AssertionError("public contract leaked private token")
        return payload

    def review_code(self, ctx: sch.LaneContext):
        n = self.code_rounds[ctx.lane.lane_id]
        self.code_rounds[ctx.lane.lane_id] += 1
        if n == 0:
            return st.ReviewerVerdict.REVISE, (FINDING,)
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


class FactoryCutoverTests(unittest.TestCase):
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

    def _start(self, actor: ScriptedActor) -> str:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:two-lane"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        run_id = "run-two-lane"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        FactoryScheduler = sch.FactoryScheduler
        scheduler = FactoryScheduler(self.store, run_id, actor, self.runtime, target)
        status = scheduler.run()
        self.assertEqual(status, st.RunStatus.COMPLETE)
        return run_id

    def test_two_dependent_lanes_seal_revise_merge_and_publish(self) -> None:
        actor = ScriptedActor(self.repo, self.runtime.path / "worktrees")
        run_id = self._start(actor)
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.MERGED)
        self.assertEqual(self.store.lane_stage(run_id, "lane-b"), st.LaneStage.MERGED)
        self.assertEqual(actor.test_rounds["lane-a"], 2)
        self.assertEqual(actor.code_rounds["lane-a"], 2)
        merges_a = [
            row
            for row in self.store.conn.execute(
                "SELECT artifact_id FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=?",
                (run_id, "lane-a", st.ArtifactKind.INTEGRATION_MERGE.value),
            )
        ]
        self.assertEqual(len(merges_a), 1)
        builder_rows = list(
            self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts "
                "WHERE run_id=? AND artifact_kind=?",
                (run_id, st.ArtifactKind.BUILDER_OUTPUT.value),
            )
        )
        for row in builder_rows:
            payload = json.loads(row[0])
            self.assertNotIn("secret-selector", json.dumps(payload))
            self.assertNotIn("vault_path", payload)
        b_builders = [
            json.loads(row[0])
            for row in self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=? "
                "ORDER BY sequence",
                (run_id, "lane-b", st.ArtifactKind.BUILDER_OUTPUT.value),
            )
        ]
        self.assertGreaterEqual(len(b_builders), 1)
        a_merge = json.loads(
            self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=?",
                (run_id, "lane-a", st.ArtifactKind.INTEGRATION_MERGE.value),
            ).fetchone()[0]
        )
        self.assertEqual(b_builders[0]["builder_base_sha"], a_merge["after_sha"])
        b_merge = json.loads(
            self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=?",
                (run_id, "lane-b", st.ArtifactKind.INTEGRATION_MERGE.value),
            ).fetchone()[0]
        )
        self.assertEqual(
            sch.durable_integration_tip(self.store, run_id), b_merge["after_sha"]
        )
        a_entries = [
            entry
            for lane_id, entry, _rev in actor.building_entries
            if lane_id == "lane-a"
        ]
        self.assertEqual(
            a_entries,
            [st.BuildingEntryKind.INITIAL, st.BuildingEntryKind.CODE_REVISE],
        )
        self._assert_private_tests_executed(run_id, "lane-a")
        self._assert_private_tests_executed(run_id, "lane-b")

    def test_death_before_building_resumes_from_sealed_stage(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:resume"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        run_id = "run-resume"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class StopBeforeBuild(ScriptedActor):
            def build(self, ctx):
                raise RuntimeError("simulated death")

        actor = StopBeforeBuild(self.repo, self.runtime.path / "worktrees")
        scheduler = sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, target
        )
        with self.assertRaises(RuntimeError):
            scheduler.run()
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.BUILDING)
        resumed = sch.FactoryScheduler(
            self.store,
            run_id,
            ScriptedActor(self.repo, self.runtime.path / "worktrees"),
            self.runtime,
            target,
        )
        self.assertEqual(resumed.run(), st.RunStatus.COMPLETE)
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.MERGED)

    def test_cli_surface_is_frozen_verbs_only(self) -> None:
        verbs = maestro.parser_verbs(maestro.build_parser())
        self.assertEqual(
            verbs,
            (
                "run start",
                "run resume",
                "run amend",
                "run status",
            ),
        )
        self.assertNotIn("retry", verbs)
        self.assertNotIn("skip", verbs)
        self.assertNotIn("abandon", verbs)
        self.assertNotIn("attempt salvage", verbs)

    def test_template_source_run_start_refuses_mismatch(self) -> None:
        code = maestro.main(
            [
                "run",
                "start",
                str(self.root / "missing.json"),
                "--repo",
                str(self.repo),
                "--main-ref",
                "refs/heads/main",
            ]
        )
        self.assertEqual(code, 3)

    def _binding_target(self) -> gitpub.TargetBinding:
        return gitpub.bind_target_worktree(self.repo, "refs/heads/main")

    def _compile(self, *, revision: int, ref: str, a_goal: str = "emit a.txt"):
        return plan_compiler.compile_plan(
            _plan_bytes(a_goal=a_goal), plan_revision=revision, plan_artifact_ref=ref
        )

    def _lane_rows(
        self, run_id: str, kind: st.ArtifactKind, lane_id: str
    ) -> list[tuple[str, str, dict]]:
        rows = []
        for artifact_id, digest, payload in self.store.conn.execute(
            "SELECT artifact_id, input_digest, payload_json FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind=? ORDER BY sequence",
            (run_id, lane_id, kind.value),
        ):
            rows.append((artifact_id, digest, json.loads(payload)))
        return rows

    def _run_rows(
        self, run_id: str, kind: st.ArtifactKind
    ) -> list[tuple[str, str, dict]]:
        rows = []
        for artifact_id, digest, payload in self.store.conn.execute(
            "SELECT artifact_id, input_digest, payload_json FROM run_artifacts "
            "WHERE run_id=? AND artifact_kind=? ORDER BY sequence",
            (run_id, kind.value),
        ):
            rows.append((artifact_id, digest, json.loads(payload)))
        return rows

    def _assert_vault_isolated(self, run_id: str) -> Path:
        vault = hv.vault_path(self.runtime.path, run_id)
        self.assertTrue(vault.is_dir())
        vault_res = vault.resolve()
        state_res = self.runtime.path.resolve()
        repo_res = self.repo.resolve()
        self.assertTrue(str(vault_res).startswith(str(state_res) + "/"))
        self.assertFalse(str(vault_res).startswith(str(repo_res) + "/"))
        self.assertNotEqual(vault_res, repo_res)
        return vault

    def _assert_private_tests_executed(self, run_id: str, lane_id: str) -> None:
        vault = self._assert_vault_isolated(run_id)
        sealed_ref = self.store.conn.execute(
            "SELECT artifact_ref FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind=? "
            "ORDER BY sequence DESC LIMIT 1",
            (run_id, lane_id, st.ArtifactKind.SEALED_TEST_BUNDLE.value),
        ).fetchone()
        self.assertIsNotNone(sealed_ref)
        commit = hv.rev_parse(vault, sealed_ref[0])
        self.assertTrue(hv.object_is_absent(self.repo, commit))
        passing = [
            payload
            for _aid, _digest, payload in self._lane_rows(
                run_id, st.ArtifactKind.CODE_REVIEW, lane_id
            )
            if payload.get("verdict") == st.ReviewerVerdict.PASS.value
        ]
        self.assertGreaterEqual(len(passing), 1)
        summary = passing[-1]["public_result_summary"]
        self.assertGreaterEqual(summary["executed"], 1)
        self.assertGreaterEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["errored"], 0)
        self.assertNotIn("secret-selector", json.dumps(passing[-1]))

    def test_create_run_replays_sqlite_before_ref(self) -> None:
        compiled = self._compile(revision=1, ref="plan:replay-sqlite")
        target = self._binding_target()
        run_id = "run-replay-sqlite"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        ref = st.integration_ref(run_id)
        _git(self.repo, "update-ref", "-d", ref)
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        self.assertEqual(
            _git(self.repo, "rev-parse", ref), target.integration_initial_sha
        )

    def test_create_run_replays_ref_before_return(self) -> None:
        compiled = self._compile(revision=1, ref="plan:replay-ref")
        target = self._binding_target()
        run_id = "run-replay-ref"
        first = sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        second = sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        self.assertEqual(first, second)
        rows = list(
            self.store.conn.execute("SELECT run_id FROM runs WHERE run_id=?", (run_id,))
        )
        self.assertEqual(len(rows), 1)

    def test_merge_epoch_from_run_created_at(self) -> None:
        actor = ScriptedActor(self.repo, self.runtime.path / "worktrees")
        run_id = self._start(actor)
        created = sch.run_row(self.store, run_id)["created_at"]
        epoch = sch.merge_epoch_seconds(created)
        merge = json.loads(
            self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=?",
                (run_id, "lane-a", st.ArtifactKind.INTEGRATION_MERGE.value),
            ).fetchone()[0]
        )
        stamp = _git(self.repo, "log", "-1", "--format=%ct", merge["after_sha"])
        self.assertEqual(int(stamp), epoch)
        parsed = datetime.fromisoformat(created)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        self.assertEqual(epoch, int(parsed.astimezone(timezone.utc).timestamp()))

    def test_amendment_refuses_published_run(self) -> None:
        actor = ScriptedActor(self.repo, self.runtime.path / "worktrees")
        run_id = self._start(actor)
        target = self._binding_target()
        amended = self._compile(revision=2, ref="plan:published", a_goal="changed a")
        with self.assertRaises(AmendmentRefused):
            sch.apply_factory_amendment(
                self.store, run_id, amended, runtime=self.runtime, target=target
            )

    def test_amendment_vs_publication_lock(self) -> None:
        compiled = self._compile(revision=1, ref="plan:race")
        target = self._binding_target()
        run_id = "run-race"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class PassActor(ScriptedActor):
            def review_tests(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

        started = threading.Event()
        outcome: dict[str, object] = {}
        original = sch.gitpub.publish_or_reconcile_locked

        def wrapped(*args, **kwargs):
            started.set()
            time.sleep(0.05)
            return original(*args, **kwargs)

        sch.gitpub.publish_or_reconcile_locked = wrapped  # type: ignore[method-assign]

        def amend() -> None:
            if not started.wait(timeout=60):
                outcome["err"] = TimeoutError("publication lock never entered")
                return
            try:
                sch.apply_factory_amendment(
                    self.store,
                    run_id,
                    self._compile(revision=2, ref="plan:race-2", a_goal="changed a"),
                    runtime=self.runtime,
                    target=target,
                )
                outcome["ok"] = True
            except AmendmentRefused as exc:
                outcome["err"] = exc

        try:
            thread = threading.Thread(target=amend)
            thread.start()
            scheduler = sch.FactoryScheduler(
                self.store,
                run_id,
                PassActor(self.repo, self.runtime.path / "worktrees"),
                self.runtime,
                target,
            )
            status = scheduler.run()
            thread.join(timeout=10)
        finally:
            sch.gitpub.publish_or_reconcile_locked = original  # type: ignore[method-assign]
        self.assertEqual(status, st.RunStatus.COMPLETE)
        self.assertIsInstance(outcome.get("err"), AmendmentRefused)
        self.assertNotIn("ok", outcome)

    def test_post_amendment_building_selects_initial(self) -> None:
        compiled = self._compile(revision=1, ref="plan:amend-build")
        target = self._binding_target()
        run_id = "run-amend-build"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class StopAfterRevise(ScriptedActor):
            def review_tests(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx):
                n = self.code_rounds[ctx.lane.lane_id]
                self.code_rounds[ctx.lane.lane_id] += 1
                if ctx.lane.lane_id == "lane-b" and n == 0:
                    return st.ReviewerVerdict.REVISE, (FINDING,)
                return st.ReviewerVerdict.PASS, ()

            def build(self, ctx):
                if (
                    ctx.lane.lane_id == "lane-b"
                    and ctx.entry_kind is st.BuildingEntryKind.CODE_REVISE
                    and ctx.plan_revision == 1
                ):
                    raise RuntimeError("stop before second build")
                return super().build(ctx)

        actor = StopAfterRevise(self.repo, self.runtime.path / "worktrees")
        scheduler = sch.FactoryScheduler(
            self.store, run_id, actor, self.runtime, target
        )
        with self.assertRaises(RuntimeError):
            scheduler.run()
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.MERGED)
        self.assertEqual(self.store.lane_stage(run_id, "lane-b"), st.LaneStage.BUILDING)
        sch.apply_factory_amendment(
            self.store,
            run_id,
            self._compile(revision=2, ref="plan:amend-build-2", a_goal="changed a"),
            runtime=self.runtime,
            target=target,
        )
        self.assertEqual(self.store.lane_stage(run_id, "lane-b"), st.LaneStage.TESTS_SEALED)

        class ResumeActor(ScriptedActor):
            def review_tests(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

        resumed = ResumeActor(self.repo, self.runtime.path / "worktrees")
        follow = sch.FactoryScheduler(self.store, run_id, resumed, self.runtime, target)
        self.assertEqual(follow.run(), st.RunStatus.COMPLETE)
        b_after = [
            (entry, rev)
            for lane_id, entry, rev in resumed.building_entries
            if lane_id == "lane-b"
        ]
        self.assertGreaterEqual(len(b_after), 1)
        self.assertEqual(b_after[0], (st.BuildingEntryKind.INITIAL, 2))

    def test_cas_before_ledger_death_resumes_unique_merge(self) -> None:
        compiled = self._compile(revision=1, ref="plan:cas-death")
        target = self._binding_target()
        run_id = "run-cas-death"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class PassActor(ScriptedActor):
            def review_tests(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

        calls = {"n": 0}
        original = sch.gitpub.merge_or_reconcile

        def wrapped(*args, **kwargs):
            payload = original(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("death after CAS")
            return payload

        sch.gitpub.merge_or_reconcile = wrapped  # type: ignore[method-assign]
        try:
            scheduler = sch.FactoryScheduler(
                self.store,
                run_id,
                PassActor(self.repo, self.runtime.path / "worktrees"),
                self.runtime,
                target,
            )
            with self.assertRaises(RuntimeError):
                scheduler.run()
        finally:
            sch.gitpub.merge_or_reconcile = original  # type: ignore[method-assign]
        self.assertEqual(
            self.store.lane_stage(run_id, "lane-a"), st.LaneStage.READY_TO_MERGE
        )
        ref = st.integration_ref(run_id)
        git_head = _git(self.repo, "rev-parse", ref)
        ledger = sch.durable_integration_tip(self.store, run_id)
        self.assertNotEqual(git_head, ledger)
        resumed = sch.FactoryScheduler(
            self.store,
            run_id,
            PassActor(self.repo, self.runtime.path / "worktrees"),
            self.runtime,
            target,
        )
        resumed.status()
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.MERGED)
        self.assertEqual(resumed.run(), st.RunStatus.COMPLETE)

    def test_amendment_after_orphaned_merge(self) -> None:
        compiled = self._compile(revision=1, ref="plan:amend-orphan")
        target = self._binding_target()
        run_id = "run-amend-orphan"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class PassActor(ScriptedActor):
            def review_tests(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

        calls = {"n": 0}
        original = sch.gitpub.merge_or_reconcile

        def wrapped(*args, **kwargs):
            payload = original(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("death after CAS")
            return payload

        sch.gitpub.merge_or_reconcile = wrapped  # type: ignore[method-assign]
        try:
            scheduler = sch.FactoryScheduler(
                self.store,
                run_id,
                PassActor(self.repo, self.runtime.path / "worktrees"),
                self.runtime,
                target,
            )
            with self.assertRaises(RuntimeError):
                scheduler.run()
        finally:
            sch.gitpub.merge_or_reconcile = original  # type: ignore[method-assign]
        self.assertEqual(
            self.store.lane_stage(run_id, "lane-a"), st.LaneStage.READY_TO_MERGE
        )
        ref = st.integration_ref(run_id)
        git_head = _git(self.repo, "rev-parse", ref)
        ledger = sch.durable_integration_tip(self.store, run_id)
        self.assertNotEqual(git_head, ledger)
        record = sch.apply_factory_amendment(
            self.store,
            run_id,
            self._compile(revision=2, ref="plan:amend-orphan-2", a_goal="changed a"),
            runtime=self.runtime,
            target=target,
        )
        self.assertEqual(record.kind, st.ArtifactKind.PLAN_AMENDMENT)
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.PLANNED)
        self.assertEqual(
            sch.durable_integration_tip(self.store, run_id),
            _git(self.repo, "rev-parse", ref),
        )
        self.assertEqual(record.payload["integration_head"], git_head)

    def test_amendment_after_pass_before_publication(self) -> None:
        compiled = self._compile(revision=1, ref="plan:pass-unpub")
        target = self._binding_target()
        run_id = "run-pass-unpub"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class PassActor(ScriptedActor):
            def review_tests(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

        original = sch.gitpub.publish_or_reconcile_locked

        def wrapped(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("stop before publication")

        sch.gitpub.publish_or_reconcile_locked = wrapped  # type: ignore[method-assign]
        try:
            scheduler = sch.FactoryScheduler(
                self.store,
                run_id,
                PassActor(self.repo, self.runtime.path / "worktrees"),
                self.runtime,
                target,
            )
            with self.assertRaises(RuntimeError):
                scheduler.run()
        finally:
            sch.gitpub.publish_or_reconcile_locked = original  # type: ignore[method-assign]
        review = sch._latest_run_artifact(
            self.store, run_id, st.ArtifactKind.FINAL_INTEGRATION_REVIEW
        )
        self.assertIsNotNone(review)
        assert review is not None
        self.assertEqual(review.payload.get("verdict"), st.ReviewerVerdict.PASS.value)
        self.assertFalse(sch._has_publication(self.store, run_id))
        record = sch.apply_factory_amendment(
            self.store,
            run_id,
            self._compile(revision=2, ref="plan:pass-unpub-2", a_goal="changed a"),
            runtime=self.runtime,
            target=target,
        )
        self.assertEqual(record.kind, st.ArtifactKind.PLAN_AMENDMENT)
        self.assertFalse(sch._has_publication(self.store, run_id))
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.PLANNED)
        self.assertEqual(self.store.lane_stage(run_id, "lane-b"), st.LaneStage.TESTS_SEALED)
        follow = sch.FactoryScheduler(
            self.store,
            run_id,
            PassActor(self.repo, self.runtime.path / "worktrees"),
            self.runtime,
            target,
        )
        self.assertEqual(follow.run(), st.RunStatus.COMPLETE)

    def test_two_dependent_lanes_twelve_step_sequence(self) -> None:
        compiled = self._compile(revision=1, ref="plan:twelve-step")
        target = self._binding_target()
        run_id = "run-twelve-step"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class TwelveStepActor(ScriptedActor):
            def __init__(self, repo, worktrees, *, fail_b: bool) -> None:
                super().__init__(repo, worktrees)
                self.fail_b = fail_b

            def write_tests(self, ctx: sch.LaneContext) -> dict:
                extra = super().write_tests(ctx)
                n = self.test_rounds[ctx.lane.lane_id]
                files = {}
                for path, body in extra["files"].items():
                    files[path] = body + "# round {0}\n".format(n)
                extra["files"] = files
                return extra

            def review_tests(self, ctx: sch.LaneContext):
                n = self.test_rounds[ctx.lane.lane_id]
                self.test_rounds[ctx.lane.lane_id] += 1
                if ctx.lane.lane_id == "lane-a" and n < 2:
                    return st.ReviewerVerdict.REVISE, (FINDING,)
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx: sch.LaneContext):
                n = self.code_rounds[ctx.lane.lane_id]
                self.code_rounds[ctx.lane.lane_id] += 1
                if ctx.lane.lane_id == "lane-a" and n == 0:
                    return st.ReviewerVerdict.REVISE, (FINDING,)
                return st.ReviewerVerdict.PASS, ()

            def build(self, ctx: sch.LaneContext) -> dict:
                if ctx.lane.lane_id == "lane-b" and self.fail_b:
                    raise RuntimeError("interrupt B before builder output")
                return super().build(ctx)

        first = TwelveStepActor(self.repo, self.runtime.path / "worktrees", fail_b=True)
        with self.assertRaises(RuntimeError):
            sch.FactoryScheduler(self.store, run_id, first, self.runtime, target).run()
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.MERGED)
        self.assertEqual(self.store.lane_stage(run_id, "lane-b"), st.LaneStage.BUILDING)
        self.assertEqual(
            self._lane_rows(run_id, st.ArtifactKind.BUILDER_OUTPUT, "lane-b"), []
        )

        a_test_reviews = self._lane_rows(run_id, st.ArtifactKind.TEST_REVIEW, "lane-a")
        self.assertEqual(len(a_test_reviews), 3)
        self.assertEqual(
            a_test_reviews[0][2]["verdict"], st.ReviewerVerdict.REVISE.value
        )
        self.assertEqual(
            a_test_reviews[1][2]["verdict"], st.ReviewerVerdict.REVISE.value
        )
        self.assertEqual(a_test_reviews[2][2]["verdict"], st.ReviewerVerdict.PASS.value)
        self.assertNotEqual(a_test_reviews[0][0], a_test_reviews[1][0])
        self.assertNotEqual(a_test_reviews[0][1], a_test_reviews[1][1])
        self.assertEqual(
            len(self._lane_rows(run_id, st.ArtifactKind.TEST_DRAFT, "lane-a")), 3
        )
        sealed = self._lane_rows(run_id, st.ArtifactKind.SEALED_TEST_BUNDLE, "lane-a")
        self.assertEqual(len(sealed), 1)
        self.assertNotIn("secret-selector", json.dumps(sealed[0][2]))
        a_code = self._lane_rows(run_id, st.ArtifactKind.CODE_REVIEW, "lane-a")
        self.assertEqual(len(a_code), 2)
        self.assertEqual(a_code[0][2]["verdict"], st.ReviewerVerdict.REVISE.value)
        self.assertEqual(a_code[-1][2]["verdict"], st.ReviewerVerdict.PASS.value)
        self._assert_private_tests_executed(run_id, "lane-a")
        a_builders = self._lane_rows(run_id, st.ArtifactKind.BUILDER_OUTPUT, "lane-a")
        self.assertEqual(len(a_builders), 2)
        for _aid, _digest, payload in a_builders:
            self.assertNotIn("secret-selector", json.dumps(payload))
            self.assertNotIn("vault_path", payload)
        self.assertEqual(
            [
                entry
                for lane_id, entry, _rev in first.building_entries
                if lane_id == "lane-a"
            ],
            [st.BuildingEntryKind.INITIAL, st.BuildingEntryKind.CODE_REVISE],
        )
        a_merges = self._lane_rows(run_id, st.ArtifactKind.INTEGRATION_MERGE, "lane-a")
        self.assertEqual(len(a_merges), 1)
        self.assertNotIn("secret-selector", _git(self.repo, "log", "--all", "-p"))

        resumed = TwelveStepActor(
            self.repo, self.runtime.path / "worktrees", fail_b=False
        )
        scheduler = sch.FactoryScheduler(
            self.store, run_id, resumed, self.runtime, target
        )
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        b_builders = self._lane_rows(run_id, st.ArtifactKind.BUILDER_OUTPUT, "lane-b")
        self.assertEqual(len(b_builders), 1)
        self.assertEqual(
            b_builders[0][2]["builder_base_sha"], a_merges[0][2]["after_sha"]
        )
        b_merges = self._lane_rows(run_id, st.ArtifactKind.INTEGRATION_MERGE, "lane-b")
        self.assertEqual(len(b_merges), 1)
        reviews = self._run_rows(run_id, st.ArtifactKind.FINAL_INTEGRATION_REVIEW)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0][2]["verdict"], st.ReviewerVerdict.PASS.value)
        pubs = self._run_rows(run_id, st.ArtifactKind.MAIN_PUBLICATION)
        self.assertEqual(len(pubs), 1)
        tip = sch.durable_integration_tip(self.store, run_id)
        self.assertEqual(pubs[0][2]["published_sha"], tip)
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), tip)
        self.assertEqual(b_merges[0][2]["after_sha"], tip)
        again = sch.FactoryScheduler(
            self.store,
            run_id,
            TwelveStepActor(self.repo, self.runtime.path / "worktrees", fail_b=False),
            self.runtime,
            target,
        ).run()
        self.assertEqual(again, st.RunStatus.COMPLETE)
        self.assertEqual(
            len(self._lane_rows(run_id, st.ArtifactKind.INTEGRATION_MERGE, "lane-a")), 1
        )
        self.assertEqual(
            len(self._lane_rows(run_id, st.ArtifactKind.INTEGRATION_MERGE, "lane-b")), 1
        )
        self.assertEqual(
            len(self._run_rows(run_id, st.ArtifactKind.FINAL_INTEGRATION_REVIEW)), 1
        )
        self.assertEqual(
            len(self._run_rows(run_id, st.ArtifactKind.MAIN_PUBLICATION)), 1
        )
        self._assert_private_tests_executed(run_id, "lane-b")

    def test_publication_refuses_external_same_sha_without_receipt(self) -> None:
        compiled = self._compile(revision=1, ref="plan:same-sha")
        target = self._binding_target()
        run_id = "run-same-sha"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )

        class PassActor(ScriptedActor):
            def review_tests(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

            def review_code(self, ctx):
                del ctx
                return st.ReviewerVerdict.PASS, ()

        original = sch.gitpub.publish_or_reconcile_locked

        def wrapped(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("stop before publication")

        sch.gitpub.publish_or_reconcile_locked = wrapped  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                sch.FactoryScheduler(
                    self.store,
                    run_id,
                    PassActor(self.repo, self.runtime.path / "worktrees"),
                    self.runtime,
                    target,
                ).run()
        finally:
            sch.gitpub.publish_or_reconcile_locked = original  # type: ignore[method-assign]
        tip = sch.durable_integration_tip(self.store, run_id)
        _git(self.repo, "update-ref", "refs/heads/main", tip)
        with self.assertRaises(gitpub.GitPublicationRefused) as raised:
            sch.FactoryScheduler(
                self.store,
                run_id,
                PassActor(self.repo, self.runtime.path / "worktrees"),
                self.runtime,
                target,
            ).run()
        self.assertEqual(raised.exception.code, "PUBLICATION_EXTERNAL_SAME_SHA")
        self.assertFalse(sch._has_publication(self.store, run_id))
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), tip)


if __name__ == "__main__":
    unittest.main()
