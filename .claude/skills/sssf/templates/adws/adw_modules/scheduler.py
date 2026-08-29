"""Nine-stage artifact-factory reducer. Only the orchestrator writes the store."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from . import code_review as cr
from . import git_publication as gitpub
from . import private_review as prv
from . import scheduler_types as st
from . import tests_chain as tc
from .lifecycle import AmendmentRefused, ArtifactRecord, ArtifactStore, RunAlreadyExists
from .runtime_state import RuntimeStateRoot

TEMPLATE_MARKERS = (
    "/.claude/skills/sssf/templates/adws",
    "/skills/sssf/templates/adws",
)


class RunRepositoryMismatch(st.KernelError):
    code = "RUN_REPOSITORY_MISMATCH"


class PublicationWorktreeLockRefused(st.KernelError):
    code = "PUBLICATION_WORKTREE_LOCK_REFUSED"


class FactoryRefused(st.KernelError):
    code = "FACTORY_REFUSED"


def classify_executing_runtime(maestro_file: Path) -> str:
    resolved = maestro_file.resolve()
    text = str(resolved)
    if any(marker in text for marker in TEMPLATE_MARKERS):
        return "template"
    if resolved.name == "maestro.py" and resolved.parent.name == "adws":
        return "deployment"
    return "template"


def git_common_dir(path: Path) -> Path:
    raw = subprocess.check_output(
        [
            "git",
            "-C",
            str(path),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        text=True,
    ).strip()
    return Path(raw).resolve()


def require_deployment(maestro_file: Path, target_root: Path) -> None:
    if classify_executing_runtime(maestro_file) != "deployment":
        raise RunRepositoryMismatch("template source cannot create a run")
    executing_root = maestro_file.resolve().parent.parent
    if git_common_dir(executing_root) != git_common_dir(target_root):
        raise RunRepositoryMismatch(
            "deployment is bound to a different Git common directory"
        )


def _loads(text: str) -> Any:
    return json.loads(text)


def _latest(
    store: ArtifactStore,
    run_id: str,
    lane_id: str,
    kind: st.ArtifactKind,
    *,
    verdict: st.ReviewerVerdict | None = None,
    plan_revision: int | None = None,
) -> Optional[ArtifactRecord]:
    sql = (
        "SELECT * FROM lane_artifacts WHERE run_id=? AND lane_id=? "
        "AND artifact_kind=? ORDER BY sequence DESC"
    )
    for row in store.conn.execute(sql, (run_id, lane_id, kind.value)):
        if plan_revision is not None and row["plan_revision"] != plan_revision:
            continue
        payload = _loads(row["payload_json"])
        if verdict is None or payload.get("verdict") == verdict.value:
            return ArtifactRecord(
                artifact_id=row["artifact_id"],
                run_id=run_id,
                lane_id=lane_id,
                sequence=row["sequence"],
                kind=kind,
                plan_revision=row["plan_revision"],
                input_digest=row["input_digest"],
                output_digest=row["output_digest"],
                artifact_ref=row["artifact_ref"],
                payload=payload,
            )
    return None


def run_row(store: ArtifactStore, run_id: str) -> Mapping[str, Any]:
    row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise FactoryRefused(f"unknown run {run_id}")
    return {key: row[key] for key in row.keys()}


def binding_from_run(row: Mapping[str, Any]) -> st.RunBinding:
    return st.RunBinding(
        runtime_state_root=row["runtime_state_root"],
        runtime_state_fingerprint=row["runtime_state_fingerprint"],
        integration_ref=row["integration_ref"],
        integration_initial_sha=row["integration_initial_sha"],
        target_repository_root=row["target_repository_root"],
        target_git_common_dir=row["target_git_common_dir"],
        target_worktree_git_dir=row["target_worktree_git_dir"],
        target_object_format=row["target_object_format"],
        target_repository_fingerprint=row["target_repository_fingerprint"],
        target_sync_journal_fingerprint=row["target_sync_journal_fingerprint"],
        target_initial_main_sha=row["target_initial_main_sha"],
        target_main_ref=row["target_main_ref"],
    )


def target_from_binding(binding: st.RunBinding) -> gitpub.TargetBinding:
    return gitpub.bind_target_worktree(
        binding.target_repository_root, binding.target_main_ref
    )


def integration_merge_payloads(
    store: ArtifactStore, run_id: str
) -> tuple[Mapping[str, Any], ...]:
    rows = store.conn.execute(
        "SELECT a.payload_json FROM lane_artifacts AS a "
        "JOIN transitions AS t "
        "ON t.run_id = a.run_id AND t.lane_id = a.lane_id "
        "AND t.artifact_id = a.artifact_id AND t.reason = 'complete_stage' "
        "WHERE a.run_id=? AND a.artifact_kind=? "
        "ORDER BY t.id ASC",
        (run_id, st.ArtifactKind.INTEGRATION_MERGE.value),
    )
    return tuple(_loads(row[0]) for row in rows)


def durable_integration_tip(store: ArtifactStore, run_id: str) -> str:
    row = run_row(store, run_id)
    return gitpub.durable_integration_tip(
        row["integration_initial_sha"], integration_merge_payloads(store, run_id)
    )


def merge_epoch_seconds(created_at: str) -> int:
    stamp = datetime.fromisoformat(created_at)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.astimezone(timezone.utc).timestamp())


def ensure_run_integration_ref(
    target: gitpub.TargetBinding, store: ArtifactStore, run_id: str
) -> str:
    tip = durable_integration_tip(store, run_id)
    gitpub.ensure_integration_ref(target, run_id, expected_tip=tip)
    return tip


def _plan_artifact_ref_for(
    store: ArtifactStore, run_id: str, plan_revision: int
) -> str:
    found = store.conn.execute(
        "SELECT plan_artifact_ref FROM plan_revisions "
        "WHERE run_id=? AND plan_revision=?",
        (run_id, plan_revision),
    ).fetchone()
    return found[0]


def _explain_ahead_merge(
    store: ArtifactStore,
    target: gitpub.TargetBinding,
    run_id: str,
    lane_id: str,
    ledger_tip: str,
    current: str,
    row: Mapping[str, Any],
) -> Optional[tuple[LaneContext, Mapping[str, Any]]]:
    lane = next(
        item for item in store.active_projection(run_id) if item.lane_id == lane_id
    )
    builder = _latest(store, run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT)
    review = _latest(
        store,
        run_id,
        lane_id,
        st.ArtifactKind.CODE_REVIEW,
        verdict=st.ReviewerVerdict.PASS,
    )
    if builder is None or review is None:
        return None
    try:
        decision = gitpub.decide_merge_action(
            changed=bool(builder.payload.get("changed", True)),
            builder_base_sha=builder.payload["builder_base_sha"],
            candidate_sha=builder.payload["candidate_sha"],
            integration_head=ledger_tip,
        )
    except gitpub.GitPublicationRefused:
        return None
    if decision.action != "MERGE":
        return None
    digest = st.ready_to_merge_input_digest(
        run_id=run_id,
        lane_id=lane_id,
        plan_revision=row["plan_revision"],
        plan_digest=row["plan_digest"],
        spec_digest=lane.spec_digest,
        projection_digest=lane.lane_projection_digest,
        builder_output_id=builder.artifact_id,
        code_review_id=review.artifact_id,
        builder_base_sha=builder.payload["builder_base_sha"],
        candidate_ref=builder.payload["candidate_ref"],
        candidate_sha=builder.payload["candidate_sha"],
        integration_head=ledger_tip,
    )
    ctx = LaneContext(
        run_id=run_id,
        lane=lane,
        plan_revision=row["plan_revision"],
        plan_digest=row["plan_digest"],
        plan_artifact_ref=_plan_artifact_ref_for(store, run_id, row["plan_revision"]),
        input_digest=digest,
        stage=st.LaneStage.READY_TO_MERGE,
        artifacts={"BUILDER_OUTPUT": builder, "CODE_REVIEW": review},
        builder_base_sha=builder.payload["builder_base_sha"],
        candidate_ref=builder.payload["candidate_ref"],
        candidate_sha=builder.payload["candidate_sha"],
        integration_head=ledger_tip,
    )
    try:
        payload = gitpub.merge_or_reconcile(
            target,
            run_id=run_id,
            lane_id=lane_id,
            stage_input_digest=digest,
            builder_artifact_id=builder.artifact_id,
            code_review_artifact_id=review.artifact_id,
            builder_base_sha=builder.payload["builder_base_sha"],
            candidate_ref=builder.payload["candidate_ref"],
            candidate_sha=builder.payload["candidate_sha"],
            before_sha=ledger_tip,
            epoch_seconds=merge_epoch_seconds(row["created_at"]),
            input_digest=digest,
        )
    except gitpub.GitPublicationRefused:
        return None
    if payload.get("after_sha") != current or payload.get("before_sha") != ledger_tip:
        return None
    payload = dict(payload)
    payload["integration_head"] = ledger_tip
    payload["input_digest"] = digest
    return ctx, payload


def reconcile_orphaned_integration_merge_locked(
    store: ArtifactStore, target: gitpub.TargetBinding, run_id: str
) -> None:
    """Caller already holds OrderedLocks 1-2 or 1-3. Does not acquire or release."""
    row = run_row(store, run_id)
    ledger_tip = durable_integration_tip(store, run_id)
    current = target.git().read_ref(st.integration_ref(run_id))
    if current is None or current == ledger_tip:
        return
    parents = target.git().commit_parents(current)
    if not parents or parents[0] != ledger_tip:
        raise FactoryRefused("integration ref is not one commit ahead of ledger")
    matches: list[tuple[LaneContext, Mapping[str, Any]]] = []
    for lane in store.active_projection(run_id):
        if store.lane_stage(run_id, lane.lane_id) is not st.LaneStage.READY_TO_MERGE:
            continue
        explained = _explain_ahead_merge(
            store, target, run_id, lane.lane_id, ledger_tip, current, row
        )
        if explained is not None:
            matches.append(explained)
    if len(matches) != 1:
        raise FactoryRefused("orphaned integration merge is not uniquely attributable")
    ctx, payload = matches[0]
    artifact = prv.make_lane_artifact(
        kind=st.ArtifactKind.INTEGRATION_MERGE,
        request=_request(ctx),
        payload=payload,
        artifact_ref=st.integration_ref(run_id),
    )
    _complete(store, ctx, artifact)


def _record_publication(
    store: ArtifactStore,
    run_id: str,
    fingerprint: str,
    payload: Mapping[str, Any],
) -> ArtifactRecord:
    row = run_row(store, run_id)
    artifact = st.RunArtifact(
        kind=st.ArtifactKind.MAIN_PUBLICATION,
        plan_revision=row["plan_revision"],
        input_digest=fingerprint,
        output_digest=st.digest_canonical(payload),
        artifact_ref=str(payload["receipt_ref"]),
        payload=payload,
    )
    return store.complete_publication(
        run_id,
        fingerprint,
        str(payload["receipt_ref"]),
        str(payload["receipt_object"]),
        str(payload["expected_before_sha"]),
        str(payload["published_sha"]),
        artifact,
    )


def _latest_run_artifact(
    store: ArtifactStore, run_id: str, kind: st.ArtifactKind
) -> Optional[ArtifactRecord]:
    row = store.conn.execute(
        "SELECT * FROM run_artifacts WHERE run_id=? AND artifact_kind=? "
        "ORDER BY sequence DESC LIMIT 1",
        (run_id, kind.value),
    ).fetchone()
    if row is None:
        return None
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        run_id=run_id,
        lane_id=None,
        sequence=row["sequence"],
        kind=kind,
        plan_revision=row["plan_revision"],
        input_digest=row["input_digest"],
        output_digest=row["output_digest"],
        artifact_ref=row["artifact_ref"],
        payload=_loads(row["payload_json"]),
    )


def _has_publication(store: ArtifactStore, run_id: str) -> bool:
    return (
        store.conn.execute(
            "SELECT 1 FROM run_artifacts WHERE run_id=? AND artifact_kind=?",
            (run_id, st.ArtifactKind.MAIN_PUBLICATION.value),
        ).fetchone()
        is not None
    )


@dataclass(frozen=True)
class LaneContext:
    run_id: str
    lane: st.LaneProjection
    plan_revision: int
    plan_digest: str
    plan_artifact_ref: str
    input_digest: str
    stage: st.LaneStage
    artifacts: Mapping[str, ArtifactRecord]
    builder_base_sha: str = ""
    candidate_ref: str = ""
    candidate_sha: str = ""
    integration_head: str = ""
    entry_kind: st.BuildingEntryKind = st.BuildingEntryKind.INITIAL
    public_contract: Mapping[str, Any] | None = None
    sealed_digest: str = ""


class StageActor(Protocol):
    def write_tests(self, ctx: LaneContext) -> Mapping[str, Any]: ...
    def review_tests(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]: ...
    def build(self, ctx: LaneContext) -> Mapping[str, Any]: ...
    def review_code(
        self, ctx: LaneContext
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]]]: ...
    def review_integration(
        self, ctx: LaneContext, lanes: Sequence[st.LaneProjection], integration_sha: str
    ) -> tuple[st.ReviewerVerdict, Sequence[Mapping[str, str]], Sequence[str]]: ...
    def publish(
        self,
        ctx: LaneContext,
        *,
        fingerprint: str,
        expected_before: str,
        published_sha: str,
    ) -> Mapping[str, Any]: ...


class OrderedLocks:
    """Lock order: (1) run mutation, (2) integration-ref, (3) target worktree."""

    def __init__(self, runtime: RuntimeStateRoot, worktree_git_dir: str) -> None:
        runtime.ensure_layout()
        self._run_path = runtime.path / "locks" / "run.lock"
        self._integration_path = runtime.path / "locks" / "integration.lock"
        self._worktree_git_dir = worktree_git_dir
        self._worktree_lock_path = os.path.join(
            worktree_git_dir, "maestro-publication.lock"
        )
        self._locals: list[threading.RLock] = []
        self._fds: list[int] = []
        self._worktree_cm: Any = None

    def acquire(self, levels: int) -> None:
        self.release()
        file_paths = [self._run_path, self._integration_path][:levels]
        local_paths = [str(path) for path in file_paths]
        if levels >= 3:
            local_paths.append(self._worktree_lock_path)
        try:
            for path in local_paths:
                lock = _process_lock(path)
                lock.acquire()
                self._locals.append(lock)
            for path in file_paths:
                path.touch(exist_ok=True)
                fd = os.open(path, os.O_RDWR)
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._fds.append(fd)
        except Exception:
            self.release()
            raise
        if levels >= 3:
            try:
                cm = gitpub.target_worktree_lock(self._worktree_git_dir)
                cm.__enter__()
                self._worktree_cm = cm
            except Exception as exc:
                self.release()
                raise PublicationWorktreeLockRefused(str(exc)) from exc

    def release(self) -> None:
        try:
            cm = self._worktree_cm
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                finally:
                    self._worktree_cm = None
        finally:
            try:
                while self._fds:
                    os.close(self._fds.pop())
            finally:
                while self._locals:
                    self._locals.pop().release()


def _process_lock(path: str) -> threading.RLock:
    key = os.path.realpath(path)
    with _PROCESS_LOCK_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _request(ctx: LaneContext) -> prv.VaultLaneRequest:
    return prv.VaultLaneRequest(
        run_id=ctx.run_id,
        lane_id=ctx.lane.lane_id,
        plan_revision=ctx.plan_revision,
        spec_digest=ctx.lane.spec_digest,
        lane_projection_digest=ctx.lane.lane_projection_digest,
        input_digest=ctx.input_digest,
    )


def _complete(
    store: ArtifactStore,
    ctx: LaneContext,
    artifact: st.LaneArtifact,
) -> ArtifactRecord:
    next_stage = st.next_stage_for(ctx.stage, artifact.kind, artifact.verdict)
    return store.complete_stage(
        ctx.run_id,
        ctx.lane.lane_id,
        ctx.stage,
        ctx.input_digest,
        artifact,
        next_stage,
    )


def _with_input_artifact_ids(
    artifact: st.LaneArtifact, ids: Sequence[str]
) -> st.LaneArtifact:
    payload = dict(artifact.payload)
    payload["input_artifact_ids"] = list(ids)
    return st.LaneArtifact(
        kind=artifact.kind,
        plan_revision=artifact.plan_revision,
        spec_digest=artifact.spec_digest,
        lane_projection_digest=artifact.lane_projection_digest,
        input_digest=artifact.input_digest,
        output_digest=st.digest_canonical(payload),
        artifact_ref=artifact.artifact_ref,
        payload=payload,
        verdict=artifact.verdict,
    )


def _record_as_lane_artifact(
    record: ArtifactRecord, lane: st.LaneProjection
) -> st.LaneArtifact:
    verdict = None
    if record.kind in (st.ArtifactKind.TEST_REVIEW, st.ArtifactKind.CODE_REVIEW):
        verdict = st.ReviewerVerdict(str(record.payload["verdict"]))
    return st.LaneArtifact(
        kind=record.kind,
        plan_revision=record.plan_revision,
        spec_digest=lane.spec_digest,
        lane_projection_digest=lane.lane_projection_digest,
        input_digest=record.input_digest,
        output_digest=record.output_digest,
        artifact_ref=record.artifact_ref,
        payload=dict(record.payload),
        verdict=verdict,
    )


class FactoryScheduler:
    """Advance every ready lane one frozen stage at a time."""

    def __init__(
        self,
        store: ArtifactStore,
        run_id: str,
        actor: StageActor,
        runtime: RuntimeStateRoot,
        target: gitpub.TargetBinding,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.actor = actor
        self.runtime = runtime
        self.target = target
        row = run_row(store, run_id)
        runtime.revalidate(row["runtime_state_fingerprint"])
        gitpub.revalidate_binding(target)
        self.locks = OrderedLocks(runtime, row["target_worktree_git_dir"])
        self._inflight: Optional[tuple[str, st.LaneStage, str, Mapping[str, Any]]] = (
            None
        )

    def run(self) -> st.RunStatus:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle_sigint)
        try:
            self._resume_orphaned_integration_merge()
            ensure_run_integration_ref(self.target, self.store, self.run_id)
            while True:
                status = self.status()
                if status in (st.RunStatus.COMPLETE, st.RunStatus.WAITING):
                    return status
                if status is st.RunStatus.PUBLISHABLE:
                    self._publish()
                    continue
                if status is st.RunStatus.INTEGRATION_REVIEW_PENDING:
                    self._final_review()
                    continue
                progressed = False
                for lane_id in self.store.ready_lane_ids(self.run_id):
                    stage = self.store.lane_stage(self.run_id, lane_id)
                    if stage is st.LaneStage.READY_TO_MERGE:
                        continue
                    self._advance(lane_id)
                    progressed = True
                merge_ready = [
                    lane_id
                    for lane_id in self.store.ready_lane_ids(self.run_id)
                    if self.store.lane_stage(self.run_id, lane_id)
                    is st.LaneStage.READY_TO_MERGE
                ]
                if merge_ready:
                    self._advance(merge_ready[0])
                    progressed = True
                if not progressed:
                    return self.status()
        except KeyboardInterrupt:
            self._pause_on_interrupt()
            return st.RunStatus.WAITING
        finally:
            signal.signal(signal.SIGINT, previous)

    def status(self) -> st.RunStatus:
        self._resume_orphaned_integration_merge()
        head = self._integration_head()
        return self.store.derive_run_status(self.run_id, head)

    def _resume_orphaned_integration_merge(self) -> None:
        self.locks.acquire(2)
        try:
            reconcile_orphaned_integration_merge_locked(
                self.store, self.target, self.run_id
            )
        finally:
            self.locks.release()

    def resume_waiting(self) -> None:
        for lane in self.store.active_projection(self.run_id):
            if (
                self.store.lane_stage(self.run_id, lane.lane_id)
                is not st.LaneStage.WAITING_FOR_USER
            ):
                continue
            wait = _latest(
                self.store, self.run_id, lane.lane_id, st.ArtifactKind.USER_WAIT
            )
            if wait is None:
                continue
            if wait.payload.get("wait_reason") != st.WaitReason.PAUSE.value:
                continue
            self.store.resume_lane(self.run_id, lane.lane_id)

    def _integration_head(self) -> str:
        return ensure_run_integration_ref(self.target, self.store, self.run_id)

    def _handle_sigint(self, _signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def _pause_on_interrupt(self) -> None:
        targets: list[tuple[str, st.LaneStage, str, Mapping[str, Any]]] = []
        if self._inflight is not None:
            targets.append(self._inflight)
        else:
            for lane in self.store.active_projection(self.run_id):
                stage = self.store.lane_stage(self.run_id, lane.lane_id)
                if stage not in st.PAUSEABLE_STAGES:
                    continue
                digest, observed = self._pause_input(lane.lane_id, stage)
                targets.append((lane.lane_id, stage, digest, observed))
        for lane_id, stage, digest, observed in targets:
            self.store.pause_lane(
                self.run_id, lane_id, stage, digest, observed=observed
            )

    def _pause_input(
        self, lane_id: str, stage: st.LaneStage
    ) -> tuple[str, Mapping[str, Any]]:
        row, lane = self._common(lane_id)
        plan_ref = self._plan_artifact_ref(row)
        common = dict(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
        )
        if stage is st.LaneStage.PLANNED:
            return (
                st.planned_input_digest(
                    **common,
                    plan_artifact_ref=plan_ref,
                    needs=lane.needs,
                    declared_outputs=lane.declared_outputs,
                ),
                {},
            )
        if stage is st.LaneStage.WRITING_TESTS:
            plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
            if plan is None:
                raise FactoryRefused("missing LANE_PLAN")
            review = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.TEST_REVIEW,
                verdict=st.ReviewerVerdict.REVISE,
            )
            return (
                st.writing_tests_input_digest(
                    **common,
                    lane_plan_id=plan.artifact_id,
                    test_review_id=review.artifact_id if review else st.NO_TEST_REVIEW,
                ),
                {},
            )
        if stage is st.LaneStage.REVIEWING_TESTS:
            plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
            draft = _latest(
                self.store, self.run_id, lane_id, st.ArtifactKind.TEST_DRAFT
            )
            if plan is None or draft is None:
                raise FactoryRefused("missing TEST_DRAFT")
            return (
                st.reviewing_tests_input_digest(
                    **common,
                    lane_plan_id=plan.artifact_id,
                    test_draft_id=draft.artifact_id,
                ),
                {},
            )
        if stage is st.LaneStage.TESTS_SEALED:
            plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
            draft = _latest(
                self.store, self.run_id, lane_id, st.ArtifactKind.TEST_DRAFT
            )
            review = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.TEST_REVIEW,
                verdict=st.ReviewerVerdict.PASS,
            )
            if plan is None or draft is None or review is None:
                raise FactoryRefused("missing sealed-test inputs")
            return (
                st.tests_sealed_input_digest(
                    **common,
                    lane_plan_id=plan.artifact_id,
                    test_draft_id=draft.artifact_id,
                    test_review_id=review.artifact_id,
                ),
                {},
            )
        if stage is st.LaneStage.BUILDING:
            plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
            sealed = _latest(
                self.store, self.run_id, lane_id, st.ArtifactKind.SEALED_TEST_BUNDLE
            )
            if plan is None or sealed is None:
                raise FactoryRefused("missing BUILDING inputs")
            revision = row["plan_revision"]
            revise = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.REVISE,
                plan_revision=revision,
            )
            invalidation = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.BASE_INVALIDATION,
                plan_revision=revision,
            )
            if invalidation is not None and (
                revise is None or invalidation.sequence > revise.sequence
            ):
                entry = st.BuildingEntryKind.BASE_INVALIDATION
            elif revise is not None:
                entry = st.BuildingEntryKind.CODE_REVISE
            else:
                entry = st.BuildingEntryKind.INITIAL
            builder_base = self._integration_head()
            receipts = []
            for dep in lane.needs:
                merge = _latest(
                    self.store, self.run_id, dep, st.ArtifactKind.INTEGRATION_MERGE
                )
                if merge is None:
                    raise FactoryRefused(f"missing dependency merge {dep}")
                receipts.append(merge)
            ids = [plan.artifact_id, sealed.artifact_id]
            ids.extend(item.artifact_id for item in receipts)
            prior_builder = st.NO_PRIOR_BUILDER
            code_review = st.NO_CODE_REVIEW
            base_invalidation = st.NO_BASE_INVALIDATION
            if entry is st.BuildingEntryKind.CODE_REVISE:
                prior = _latest(
                    self.store,
                    self.run_id,
                    lane_id,
                    st.ArtifactKind.BUILDER_OUTPUT,
                    plan_revision=revision,
                )
                if prior is None or revise is None:
                    raise FactoryRefused("CODE_REVISE missing prior artifacts")
                prior_builder = prior.artifact_id
                code_review = revise.artifact_id
                ids.extend([prior_builder, code_review])
            elif entry is st.BuildingEntryKind.BASE_INVALIDATION:
                prior = _latest(
                    self.store,
                    self.run_id,
                    lane_id,
                    st.ArtifactKind.BUILDER_OUTPUT,
                    plan_revision=revision,
                )
                passing = _latest(
                    self.store,
                    self.run_id,
                    lane_id,
                    st.ArtifactKind.CODE_REVIEW,
                    verdict=st.ReviewerVerdict.PASS,
                    plan_revision=revision,
                )
                if prior is None or passing is None or invalidation is None:
                    raise FactoryRefused("BASE_INVALIDATION missing artifacts")
                prior_builder = prior.artifact_id
                code_review = passing.artifact_id
                base_invalidation = invalidation.artifact_id
                ids.extend([prior_builder, code_review, base_invalidation])
            digest = st.building_input_digest(
                **common,
                input_artifact_ids=ids,
                entry_kind=entry,
                builder_base_sha=builder_base,
                prior_builder=prior_builder,
                code_review=code_review,
                base_invalidation=base_invalidation,
            )
            return digest, {
                "builder_base_sha": builder_base,
                "entry_kind": entry.value,
            }
        if stage is st.LaneStage.REVIEWING_CODE:
            plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
            sealed = _latest(
                self.store, self.run_id, lane_id, st.ArtifactKind.SEALED_TEST_BUNDLE
            )
            builder = _latest(
                self.store, self.run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            if plan is None or sealed is None or builder is None:
                raise FactoryRefused("missing REVIEWING_CODE inputs")
            return (
                st.reviewing_code_input_digest(
                    **common,
                    lane_plan_id=plan.artifact_id,
                    sealed_bundle_id=sealed.artifact_id,
                    builder_output_id=builder.artifact_id,
                    builder_base_sha=builder.payload["builder_base_sha"],
                    candidate_ref=builder.payload["candidate_ref"],
                    candidate_sha=builder.payload["candidate_sha"],
                ),
                {},
            )
        if stage is st.LaneStage.READY_TO_MERGE:
            builder = _latest(
                self.store, self.run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
            )
            review = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.PASS,
            )
            if builder is None or review is None:
                raise FactoryRefused("missing READY_TO_MERGE inputs")
            head = self._integration_head()
            digest = st.ready_to_merge_input_digest(
                **common,
                builder_output_id=builder.artifact_id,
                code_review_id=review.artifact_id,
                builder_base_sha=builder.payload["builder_base_sha"],
                candidate_ref=builder.payload["candidate_ref"],
                candidate_sha=builder.payload["candidate_sha"],
                integration_head=head,
            )
            return digest, {"integration_head": head}
        raise FactoryRefused(f"not pauseable {stage.value}")

    def _advance(self, lane_id: str) -> None:
        stage = self.store.lane_stage(self.run_id, lane_id)
        digest, observed = self._pause_input(lane_id, stage)
        self._inflight = (lane_id, stage, digest, observed)
        try:
            if stage is st.LaneStage.READY_TO_MERGE:
                self.locks.acquire(2)
                try:
                    self._ready_to_merge(lane_id)
                finally:
                    self.locks.release()
                return
            if stage is st.LaneStage.BUILDING:
                self.locks.acquire(2)
                try:
                    self._building(lane_id)
                finally:
                    self.locks.release()
                return
            dispatch = {
                st.LaneStage.PLANNED: self._planned,
                st.LaneStage.WRITING_TESTS: self._writing_tests,
                st.LaneStage.REVIEWING_TESTS: self._reviewing_tests,
                st.LaneStage.TESTS_SEALED: self._tests_sealed,
                st.LaneStage.REVIEWING_CODE: self._reviewing_code,
            }
            dispatch[stage](lane_id)
        finally:
            self._inflight = None

    def _common(self, lane_id: str) -> tuple[Mapping[str, Any], st.LaneProjection]:
        row = run_row(self.store, self.run_id)
        projection = next(
            lane
            for lane in self.store.active_projection(self.run_id)
            if lane.lane_id == lane_id
        )
        return row, projection

    def _plan_artifact_ref(self, row: Mapping[str, Any]) -> str:
        return _plan_artifact_ref_for(self.store, self.run_id, row["plan_revision"])

    def _planned(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        digest = st.planned_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            plan_artifact_ref=self._plan_artifact_ref(row),
            needs=lane.needs,
            declared_outputs=lane.declared_outputs,
        )
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.PLANNED,
            artifacts={},
        )
        payload = {
            "declared_outputs": list(lane.declared_outputs),
            "input_artifact_ids": [],
            "input_digest": digest,
            "needs": list(lane.needs),
            "plan_artifact_ref": ctx.plan_artifact_ref,
        }
        artifact = prv.make_lane_artifact(
            kind=st.ArtifactKind.LANE_PLAN,
            request=_request(ctx),
            payload=payload,
            artifact_ref=f"lane-plan:{self.run_id}:{lane_id}:{digest}",
        )
        _complete(self.store, ctx, artifact)

    def _writing_tests(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
        if plan is None:
            raise FactoryRefused("missing LANE_PLAN")
        review = _latest(
            self.store,
            self.run_id,
            lane_id,
            st.ArtifactKind.TEST_REVIEW,
            verdict=st.ReviewerVerdict.REVISE,
        )
        review_id = review.artifact_id if review is not None else st.NO_TEST_REVIEW
        digest = st.writing_tests_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            lane_plan_id=plan.artifact_id,
            test_review_id=review_id,
        )
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.WRITING_TESTS,
            artifacts={
                "LANE_PLAN": plan,
                **({"TEST_REVIEW": review} if review else {}),
            },
        )
        extra = dict(self.actor.write_tests(ctx))
        files = extra.get("files") or extra.get("private_files") or {}
        if not files:
            raise FactoryRefused("write_tests produced no private files")
        contract = prv.public_contract(
            acceptance_criteria=lane.public_acceptance,
            declared_outputs=lane.declared_outputs,
        )
        artifact = tc.write_test_draft(
            request=_request(ctx),
            state_root=self.runtime.path,
            run_repo=Path(self.target.target_repository_root),
            integration_ref=st.integration_ref(self.run_id),
            files=files,
            public_contract=contract,
            worktrees_root=self.runtime.path / "worktrees",
        )
        _complete(
            self.store,
            ctx,
            _with_input_artifact_ids(artifact, [plan.artifact_id, review_id]),
        )

    def _reviewing_tests(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
        draft = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.TEST_DRAFT)
        if plan is None or draft is None:
            raise FactoryRefused("missing TEST_DRAFT")
        digest = st.reviewing_tests_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            lane_plan_id=plan.artifact_id,
            test_draft_id=draft.artifact_id,
        )
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.REVIEWING_TESTS,
            artifacts={"LANE_PLAN": plan, "TEST_DRAFT": draft},
            public_contract=draft.payload.get("public_contract"),
        )
        verdict, findings = self.actor.review_tests(ctx)
        draft_artifact = _record_as_lane_artifact(draft, lane)
        tokens = tc.draft_private_tokens(
            state_root=self.runtime.path,
            run_id=self.run_id,
            draft=draft_artifact,
        )
        artifact = tc.review_test_draft(
            request=_request(ctx),
            verdict=verdict,
            findings=findings,
            test_draft=draft_artifact,
            private_tokens=tokens,
        )
        _complete(
            self.store,
            ctx,
            _with_input_artifact_ids(artifact, [plan.artifact_id, draft.artifact_id]),
        )

    def _tests_sealed(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
        draft = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.TEST_DRAFT)
        review = _latest(
            self.store,
            self.run_id,
            lane_id,
            st.ArtifactKind.TEST_REVIEW,
            verdict=st.ReviewerVerdict.PASS,
        )
        if plan is None or draft is None or review is None:
            raise FactoryRefused("missing sealed-test inputs")
        digest = st.tests_sealed_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            lane_plan_id=plan.artifact_id,
            test_draft_id=draft.artifact_id,
            test_review_id=review.artifact_id,
        )
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.TESTS_SEALED,
            artifacts={"LANE_PLAN": plan, "TEST_DRAFT": draft, "TEST_REVIEW": review},
            public_contract=draft.payload.get("public_contract"),
        )
        artifact = tc.seal_accepted_tests(
            request=_request(ctx),
            state_root=self.runtime.path,
            run_repo=Path(self.target.target_repository_root),
            builder_worktree=None,
            test_draft=_record_as_lane_artifact(draft, lane),
            test_review=_record_as_lane_artifact(review, lane),
        )
        _complete(
            self.store,
            ctx,
            _with_input_artifact_ids(
                artifact, [plan.artifact_id, draft.artifact_id, review.artifact_id]
            ),
        )

    def _building(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
        sealed = _latest(
            self.store, self.run_id, lane_id, st.ArtifactKind.SEALED_TEST_BUNDLE
        )
        if plan is None or sealed is None:
            raise FactoryRefused("missing BUILDING inputs")
        revision = row["plan_revision"]
        revise = _latest(
            self.store,
            self.run_id,
            lane_id,
            st.ArtifactKind.CODE_REVIEW,
            verdict=st.ReviewerVerdict.REVISE,
            plan_revision=revision,
        )
        invalidation = _latest(
            self.store,
            self.run_id,
            lane_id,
            st.ArtifactKind.BASE_INVALIDATION,
            plan_revision=revision,
        )
        if invalidation is not None and (
            revise is None or invalidation.sequence > revise.sequence
        ):
            entry = st.BuildingEntryKind.BASE_INVALIDATION
        elif revise is not None:
            entry = st.BuildingEntryKind.CODE_REVISE
        else:
            entry = st.BuildingEntryKind.INITIAL
        builder_base = self._integration_head()
        receipts = []
        for dep in lane.needs:
            merge = _latest(
                self.store, self.run_id, dep, st.ArtifactKind.INTEGRATION_MERGE
            )
            if merge is None:
                raise FactoryRefused(f"missing dependency merge {dep}")
            receipts.append(merge)
        ids = [plan.artifact_id, sealed.artifact_id]
        ids.extend(item.artifact_id for item in receipts)
        prior_builder = st.NO_PRIOR_BUILDER
        code_review = st.NO_CODE_REVIEW
        base_invalidation = st.NO_BASE_INVALIDATION
        if entry is st.BuildingEntryKind.CODE_REVISE:
            prior = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.BUILDER_OUTPUT,
                plan_revision=revision,
            )
            if prior is None or revise is None:
                raise FactoryRefused("CODE_REVISE missing prior artifacts")
            prior_builder = prior.artifact_id
            code_review = revise.artifact_id
            ids.extend([prior_builder, code_review])
        elif entry is st.BuildingEntryKind.BASE_INVALIDATION:
            prior = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.BUILDER_OUTPUT,
                plan_revision=revision,
            )
            passing = _latest(
                self.store,
                self.run_id,
                lane_id,
                st.ArtifactKind.CODE_REVIEW,
                verdict=st.ReviewerVerdict.PASS,
                plan_revision=revision,
            )
            if prior is None or passing is None or invalidation is None:
                raise FactoryRefused("BASE_INVALIDATION missing artifacts")
            prior_builder = prior.artifact_id
            code_review = passing.artifact_id
            base_invalidation = invalidation.artifact_id
            ids.extend([prior_builder, code_review, base_invalidation])
        digest = st.building_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            input_artifact_ids=ids,
            entry_kind=entry,
            builder_base_sha=builder_base,
            prior_builder=prior_builder,
            code_review=code_review,
            base_invalidation=base_invalidation,
        )
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.BUILDING,
            artifacts={"LANE_PLAN": plan, "SEALED_TEST_BUNDLE": sealed},
            builder_base_sha=builder_base,
            entry_kind=entry,
            public_contract=sealed.payload.get("public_contract"),
            sealed_digest=str(sealed.payload.get("sealed_digest") or ""),
        )
        extra = dict(self.actor.build(ctx))
        candidate_sha = extra["candidate_sha"]
        changed = bool(extra.get("changed", True))
        admitted = gitpub.admit_candidate(
            self.target,
            run_id=self.run_id,
            lane_id=lane_id,
            input_digest=digest,
            builder_base_sha=builder_base,
            candidate_sha=candidate_sha,
            changed=changed,
            declared_outputs=lane.declared_outputs,
        )
        builder_payload = {
            "builder_base_sha": admitted["builder_base_sha"],
            "candidate_ref": admitted["candidate_ref"],
            "candidate_sha": admitted["candidate_sha"],
            "changed": admitted["changed"],
            "entry_kind": entry.value,
            "input_artifact_ids": ids,
            "input_digest": digest,
            "plan_revision": row["plan_revision"],
            "sealed_test_digest": ctx.sealed_digest,
            "spec_digest": lane.spec_digest,
            "projection_digest": lane.lane_projection_digest,
            "tree_delta": admitted.get("tree_delta") or [],
        }
        prv.refuse_private_leak(
            builder_payload,
            prv.collect_private_tokens(extra=tuple(extra.get("private_tokens") or ())),
        )
        artifact = prv.make_lane_artifact(
            kind=st.ArtifactKind.BUILDER_OUTPUT,
            request=_request(ctx),
            payload=builder_payload,
            artifact_ref=admitted["candidate_ref"],
        )
        _complete(self.store, ctx, artifact)

    def _reviewing_code(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
        sealed = _latest(
            self.store, self.run_id, lane_id, st.ArtifactKind.SEALED_TEST_BUNDLE
        )
        builder = _latest(
            self.store, self.run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
        )
        if plan is None or sealed is None or builder is None:
            raise FactoryRefused("missing REVIEWING_CODE inputs")
        digest = st.reviewing_code_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            lane_plan_id=plan.artifact_id,
            sealed_bundle_id=sealed.artifact_id,
            builder_output_id=builder.artifact_id,
            builder_base_sha=builder.payload["builder_base_sha"],
            candidate_ref=builder.payload["candidate_ref"],
            candidate_sha=builder.payload["candidate_sha"],
        )
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.REVIEWING_CODE,
            artifacts={
                "LANE_PLAN": plan,
                "SEALED_TEST_BUNDLE": sealed,
                "BUILDER_OUTPUT": builder,
            },
            builder_base_sha=builder.payload["builder_base_sha"],
            candidate_ref=builder.payload["candidate_ref"],
            candidate_sha=builder.payload["candidate_sha"],
            sealed_digest=str(sealed.payload.get("sealed_digest") or ""),
        )
        verdict, findings = self.actor.review_code(ctx)
        constraints = tuple(lane.public_acceptance) or ("produce declared outputs",)
        artifact = cr.review_builder_output(
            request=_request(ctx),
            state_root=self.runtime.path,
            candidate_repo=Path(self.target.target_repository_root),
            candidate_sha=builder.payload["candidate_sha"],
            candidate_ref=builder.payload["candidate_ref"],
            builder_base_sha=builder.payload["builder_base_sha"],
            sealed_bundle=_record_as_lane_artifact(sealed, lane),
            verdict=verdict,
            findings=findings,
            scratch_root=self.runtime.path / "worktrees",
            architecture_constraints=constraints,
        )
        _complete(
            self.store,
            ctx,
            _with_input_artifact_ids(
                artifact, [plan.artifact_id, sealed.artifact_id, builder.artifact_id]
            ),
        )

    def _ready_to_merge(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        builder = _latest(
            self.store, self.run_id, lane_id, st.ArtifactKind.BUILDER_OUTPUT
        )
        review = _latest(
            self.store,
            self.run_id,
            lane_id,
            st.ArtifactKind.CODE_REVIEW,
            verdict=st.ReviewerVerdict.PASS,
        )
        if builder is None or review is None:
            raise FactoryRefused("missing READY_TO_MERGE inputs")
        head = self._integration_head()
        decision = gitpub.decide_merge_action(
            changed=bool(builder.payload.get("changed", True)),
            builder_base_sha=builder.payload["builder_base_sha"],
            candidate_sha=builder.payload["candidate_sha"],
            integration_head=head,
        )
        if decision.action == "BASE_INVALIDATION":
            digest = st.base_invalidation_input_digest(
                run_id=self.run_id,
                lane_id=lane_id,
                plan_revision=row["plan_revision"],
                plan_digest=row["plan_digest"],
                spec_digest=lane.spec_digest,
                projection_digest=lane.lane_projection_digest,
                builder_output_id=builder.artifact_id,
                code_review_id=review.artifact_id,
                stale_builder_base_sha=builder.payload["builder_base_sha"],
                stale_candidate_sha=builder.payload["candidate_sha"],
                integration_head=head,
            )
            ctx = LaneContext(
                run_id=self.run_id,
                lane=lane,
                plan_revision=row["plan_revision"],
                plan_digest=row["plan_digest"],
                plan_artifact_ref=self._plan_artifact_ref(row),
                input_digest=digest,
                stage=st.LaneStage.READY_TO_MERGE,
                artifacts={"BUILDER_OUTPUT": builder, "CODE_REVIEW": review},
                integration_head=head,
            )
            payload = gitpub.base_invalidation_payload(
                stale_builder_output_artifact_id=builder.artifact_id,
                stale_code_review_artifact_id=review.artifact_id,
                stale_builder_base_sha=builder.payload["builder_base_sha"],
                stale_candidate_sha=builder.payload["candidate_sha"],
                observed_integration_head=head,
                input_digest=digest,
            )
            payload["integration_head"] = head
            artifact = prv.make_lane_artifact(
                kind=st.ArtifactKind.BASE_INVALIDATION,
                request=_request(ctx),
                payload=payload,
                artifact_ref=f"base-invalidation:{digest}",
            )
            _complete(self.store, ctx, artifact)
            return
        digest = st.ready_to_merge_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            builder_output_id=builder.artifact_id,
            code_review_id=review.artifact_id,
            builder_base_sha=builder.payload["builder_base_sha"],
            candidate_ref=builder.payload["candidate_ref"],
            candidate_sha=builder.payload["candidate_sha"],
            integration_head=head,
        )
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.READY_TO_MERGE,
            artifacts={"BUILDER_OUTPUT": builder, "CODE_REVIEW": review},
            builder_base_sha=builder.payload["builder_base_sha"],
            candidate_ref=builder.payload["candidate_ref"],
            candidate_sha=builder.payload["candidate_sha"],
            integration_head=head,
        )
        if decision.action == "REVALIDATE":
            payload = gitpub.revalidate_zero_delta(
                self.target,
                builder_artifact_id=builder.artifact_id,
                code_review_artifact_id=review.artifact_id,
                builder_base_sha=builder.payload["builder_base_sha"],
                candidate_ref=builder.payload["candidate_ref"],
                candidate_sha=builder.payload["candidate_sha"],
                input_digest=digest,
            )
        else:
            payload = gitpub.merge_or_reconcile(
                self.target,
                run_id=self.run_id,
                lane_id=lane_id,
                stage_input_digest=digest,
                builder_artifact_id=builder.artifact_id,
                code_review_artifact_id=review.artifact_id,
                builder_base_sha=builder.payload["builder_base_sha"],
                candidate_ref=builder.payload["candidate_ref"],
                candidate_sha=builder.payload["candidate_sha"],
                before_sha=head,
                epoch_seconds=merge_epoch_seconds(row["created_at"]),
                input_digest=digest,
            )
        payload = dict(payload)
        payload["integration_head"] = head
        payload["input_digest"] = digest
        artifact = prv.make_lane_artifact(
            kind=st.ArtifactKind.INTEGRATION_MERGE,
            request=_request(ctx),
            payload=payload,
            artifact_ref=st.integration_ref(self.run_id),
        )
        _complete(self.store, ctx, artifact)

    def _final_review(self) -> None:
        self.locks.acquire(2)
        try:
            head = self._integration_head()
            fingerprint = self.store.active_final_review_fingerprint(self.run_id, head)
            row = run_row(self.store, self.run_id)
            lanes = self.store.active_projection(self.run_id)
            ctx = LaneContext(
                run_id=self.run_id,
                lane=lanes[0],
                plan_revision=row["plan_revision"],
                plan_digest=row["plan_digest"],
                plan_artifact_ref=self._plan_artifact_ref(row),
                input_digest=fingerprint,
                stage=st.LaneStage.MERGED,
                artifacts={},
                integration_head=head,
            )
            observed_main = self.target.git().rev_parse(row["target_main_ref"])
            verdict, findings, affected = self.actor.review_integration(
                ctx, lanes, head
            )
            checked = prv.actionable_findings(verdict, findings)
            payload = {
                "affected_lanes": list(affected)
                if verdict is st.ReviewerVerdict.REVISE
                else [],
                "findings": list(checked),
                "input_digest": fingerprint,
                "integration_sha": head,
                "observed_target_main_sha": observed_main,
                "verdict": verdict.value,
            }
            artifact = st.RunArtifact(
                kind=st.ArtifactKind.FINAL_INTEGRATION_REVIEW,
                plan_revision=row["plan_revision"],
                input_digest=fingerprint,
                output_digest=st.digest_canonical(payload),
                artifact_ref=f"final-review:{fingerprint}",
                payload=payload,
                verdict=verdict,
            )
            self.store.complete_final_review(
                self.run_id,
                fingerprint,
                head,
                observed_main,
                artifact,
                payload["affected_lanes"],
            )
        finally:
            self.locks.release()

    def _publish(self) -> None:
        self.locks.acquire(3)
        try:
            head = self._integration_head()
            fingerprint = self.store.active_final_review_fingerprint(self.run_id, head)
            row = run_row(self.store, self.run_id)
            review = _latest_run_artifact(
                self.store, self.run_id, st.ArtifactKind.FINAL_INTEGRATION_REVIEW
            )
            if review is None:
                raise FactoryRefused("no final review")
            expected_before = self.target.git().rev_parse(row["target_main_ref"])
            payload = gitpub.publish_or_reconcile_locked(
                self.target,
                run_id=self.run_id,
                review_input_fingerprint=fingerprint,
                final_review_artifact_id=review.artifact_id,
                expected_before_sha=expected_before,
                reviewed_integration_sha=head,
            )
            _record_publication(self.store, self.run_id, fingerprint, payload)
        finally:
            self.locks.release()


def create_factory_run(
    *,
    store: ArtifactStore,
    run_id: str,
    compiled: st.CompiledPlan,
    runtime: RuntimeStateRoot,
    target: gitpub.TargetBinding,
) -> st.RunBinding:
    runtime.ensure_layout()
    binding = st.RunBinding(
        runtime_state_root=str(runtime.path),
        runtime_state_fingerprint=runtime.fingerprint,
        integration_ref=st.integration_ref(run_id),
        integration_initial_sha=target.integration_initial_sha,
        target_repository_root=target.target_repository_root,
        target_git_common_dir=target.target_git_common_dir,
        target_worktree_git_dir=target.target_worktree_git_dir,
        target_object_format=target.target_object_format,
        target_repository_fingerprint=target.target_repository_fingerprint,
        target_sync_journal_fingerprint=target.target_sync_journal_fingerprint,
        target_initial_main_sha=target.target_initial_main_sha,
        target_main_ref=target.target_main_ref,
    )
    try:
        store.create_run(run_id, compiled, binding)
    except RunAlreadyExists:
        existing = binding_from_run(run_row(store, run_id))
        if existing != binding:
            raise
    ensure_run_integration_ref(target, store, run_id)
    return binding


def amendment_resets(
    store: ArtifactStore,
    run_id: str,
    compiled: st.CompiledPlan,
) -> tuple[st.LaneReset, ...]:
    row = run_row(store, run_id)
    old = {lane.lane_id: lane for lane in store.active_projection(run_id)}
    resets = []
    for lane in compiled.lanes:
        previous = old.get(lane.lane_id)
        if previous is None:
            resets.append(
                st.LaneReset(lane.lane_id, st.LaneStage.PLANNED, st.LaneStage.PLANNED)
            )
            continue
        changed = previous.lane_projection_digest != lane.lane_projection_digest
        current = store.lane_stage(run_id, lane.lane_id)
        wait = None
        if current is st.LaneStage.WAITING_FOR_USER:
            wait_row = _latest(store, run_id, lane.lane_id, st.ArtifactKind.USER_WAIT)
            if wait_row is not None:
                wait = st.WaitReason(wait_row.payload["wait_reason"])
        target = st.amendment_reset_stage(current, changed=changed, wait_reason=wait)
        resets.append(st.LaneReset(lane.lane_id, current, target))
    del row
    return tuple(sorted(resets, key=lambda item: item.lane_id))


def apply_factory_amendment(
    store: ArtifactStore,
    run_id: str,
    compiled: st.CompiledPlan,
    *,
    runtime: RuntimeStateRoot,
    target: gitpub.TargetBinding,
    retained_inputs: Sequence[Mapping[str, Any]] = (),
    invalidated_inputs: Sequence[Mapping[str, Any]] = (),
) -> ArtifactRecord:
    row = run_row(store, run_id)
    runtime.revalidate(row["runtime_state_fingerprint"])
    gitpub.revalidate_binding(target)
    locks = OrderedLocks(runtime, row["target_worktree_git_dir"])
    locks.acquire(3)
    try:
        review = _latest_run_artifact(
            store, run_id, st.ArtifactKind.FINAL_INTEGRATION_REVIEW
        )
        if (
            review is not None
            and review.payload.get("verdict") == st.ReviewerVerdict.PASS.value
        ):
            recon = gitpub.reconcile_publication_if_present_locked(
                target,
                run_id=run_id,
                review_input_fingerprint=review.input_digest,
                final_review_artifact_id=review.artifact_id,
                expected_before_sha=str(review.payload["observed_target_main_sha"]),
                reviewed_integration_sha=str(review.payload["integration_sha"]),
            )
            if recon is not None:
                _record_publication(store, run_id, review.input_digest, recon)
                raise AmendmentRefused("publication/in-progress")
        if _has_publication(store, run_id):
            raise AmendmentRefused("published runs are immutable")
        reconcile_orphaned_integration_merge_locked(store, target, run_id)
        tip = ensure_run_integration_ref(target, store, run_id)
        resets = amendment_resets(store, run_id, compiled)
        payload = {
            "input_digest": "",
            "invalidated_inputs": list(invalidated_inputs),
            "new_plan_digest": compiled.plan_digest,
            "new_plan_revision": compiled.plan_revision,
            "prior_plan_digest": row["plan_digest"],
            "prior_plan_revision": row["plan_revision"],
            "projection": [
                {
                    "declared_outputs": list(lane.declared_outputs),
                    "lane_id": lane.lane_id,
                    "lane_projection_digest": lane.lane_projection_digest,
                    "needs": list(lane.needs),
                    "spec_digest": lane.spec_digest,
                }
                for lane in compiled.lanes
            ],
            "resets": [
                {
                    "from_stage": item.from_stage.value,
                    "lane_id": item.lane_id,
                    "to_stage": item.to_stage.value,
                }
                for item in resets
            ],
            "retained_inputs": list(retained_inputs),
            "final_review_artifact_id": (
                review.artifact_id if review is not None else st.NO_FINAL_REVIEW
            ),
            "integration_head": tip,
        }
        digest = st.digest_canonical(
            {k: payload[k] for k in payload if k != "input_digest"}
        )
        payload["input_digest"] = digest
        artifact = st.RunArtifact(
            kind=st.ArtifactKind.PLAN_AMENDMENT,
            plan_revision=compiled.plan_revision,
            input_digest=digest,
            output_digest=st.digest_canonical(payload),
            artifact_ref=compiled.plan_artifact_ref,
            payload=payload,
        )
        return store.apply_amendment(
            run_id, row["plan_revision"], compiled, artifact, resets
        )
    finally:
        locks.release()


# Transport-only launch failure used by maestro when a pane cannot start.
class LaunchFailed(RuntimeError):
    def __init__(self, detail: str, *, pane_created: bool = False) -> None:
        super().__init__(detail)
        self.pane_created = pane_created
        self.detail = detail
