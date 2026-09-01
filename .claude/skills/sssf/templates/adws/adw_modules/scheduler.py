"""Nine-stage artifact-factory reducer. Only the orchestrator writes the store."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Protocol

from . import code_review as cr
from . import git_publication as gitpub
from . import hidden_vault as hv
from . import private_review as prv
from . import runner_resolution as rr
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


class DraftMinCasesRefused(FactoryRefused):
    code = "DRAFT_MIN_CASES"

    def __init__(self, collected: int, min_cases: int) -> None:
        self.collected = collected
        self.min_cases = min_cases
        super().__init__("collected {0}, min_cases {1}".format(collected, min_cases))


class DraftCollectionRefused(FactoryRefused):
    code = "DRAFT_COLLECTION_REFUSED"



class TypedTestOutputsRefused(FactoryRefused):
    code = "TYPED_TEST_OUTPUTS"


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
    projection_digest: str | None = None,
) -> Optional[ArtifactRecord]:
    sql = (
        "SELECT * FROM lane_artifacts WHERE run_id=? AND lane_id=? "
        "AND artifact_kind=? ORDER BY sequence DESC"
    )
    for row in store.conn.execute(sql, (run_id, lane_id, kind.value)):
        if plan_revision is not None and row["plan_revision"] != plan_revision:
            continue
        if (
            projection_digest is not None
            and row["lane_projection_digest"] != projection_digest
        ):
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



def _writing_tests_predecessors(
    store: ArtifactStore, run_id: str, lane_id: str
) -> tuple[Optional[ArtifactRecord], Optional[ArtifactRecord], str, str]:
    review = _latest(
        store,
        run_id,
        lane_id,
        st.ArtifactKind.TEST_REVIEW,
        verdict=st.ReviewerVerdict.REVISE,
    )
    invalidation = _latest(
        store, run_id, lane_id, st.ArtifactKind.TEST_INVALIDATION
    )
    draft = _latest(store, run_id, lane_id, st.ArtifactKind.TEST_DRAFT)
    review_id = review.artifact_id if review is not None else st.NO_TEST_REVIEW
    invalidation_id = st.active_test_invalidation_id(
        invalidation_id=invalidation.artifact_id if invalidation is not None else None,
        invalidation_sequence=invalidation.sequence if invalidation is not None else None,
        draft_sequence=draft.sequence if draft is not None else None,
    )
    active = invalidation if invalidation_id != st.NO_TEST_INVALIDATION else None
    return review, active, review_id, invalidation_id



def run_row(store: ArtifactStore, run_id: str) -> Mapping[str, Any]:
    row = store.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise FactoryRefused(f"unknown run {run_id}")
    return {key: row[key] for key in row.keys()}


def runs_for_target(
    store: ArtifactStore, *, repository_fingerprint: str, main_ref: str
) -> tuple[Mapping[str, Any], ...]:
    """Every run bound to one target repository identity and main ref.

    The durable half of deterministic run selection: the caller narrows these
    rows further by plan artifact ref / compiled digest and by derived status.
    Ordered by creation so a refusal names the same ids in the same order.
    """
    rows = store.conn.execute(
        "SELECT * FROM runs WHERE target_repository_fingerprint=? "
        "AND target_main_ref=? ORDER BY created_at, run_id",
        (repository_fingerprint, main_ref),
    ).fetchall()
    return tuple({key: row[key] for key in row.keys()} for row in rows)


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
    return gitpub.restore_target_initial_main_sha(
        gitpub.restore_integration_sha(
            gitpub.bind_target_worktree(
                binding.target_repository_root, binding.target_main_ref
            ),
            binding.integration_initial_sha,
        ),
        binding.target_initial_main_sha,
    )


def integration_merge_payloads(
    store: ArtifactStore, run_id: str
) -> tuple[Mapping[str, Any], ...]:
    return store.integration_merge_payloads(run_id)


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


UNSAFE_LEGACY_REBASE_KINDS = frozenset(
    {
        st.ArtifactKind.BUILDER_OUTPUT,
        st.ArtifactKind.CODE_REVIEW,
        st.ArtifactKind.INTEGRATION_MERGE,
        st.ArtifactKind.BASE_INVALIDATION,
        st.ArtifactKind.FINAL_INTEGRATION_REVIEW,
        st.ArtifactKind.MAIN_PUBLICATION,
    }
)


def recorded_artifact_kinds(
    store: ArtifactStore, run_id: str
) -> tuple[st.ArtifactKind, ...]:
    kinds: list[st.ArtifactKind] = []
    for row in store.conn.execute(
        "SELECT artifact_kind FROM lane_artifacts WHERE run_id=?",
        (run_id,),
    ):
        kinds.append(st.ArtifactKind(row[0]))
    for row in store.conn.execute(
        "SELECT artifact_kind FROM run_artifacts WHERE run_id=?",
        (run_id,),
    ):
        kinds.append(st.ArtifactKind(row[0]))
    return tuple(kinds)


def legacy_integration_correction_decision(
    *,
    stored_sha: str,
    declared_sha: str,
    lane_stages: Sequence[st.LaneStage],
    artifact_kinds: Sequence[st.ArtifactKind],
) -> str:
    stored = st.require_git_sha(stored_sha, name="stored_sha")
    declared = st.require_git_sha(declared_sha, name="declared_sha")
    if stored == declared:
        return "noop"
    if st.LaneStage.REVIEWING_TESTS in lane_stages:
        return "defer"
    if any(kind in UNSAFE_LEGACY_REBASE_KINDS for kind in artifact_kinds):
        return "refuse"
    return "migrate"


def legacy_retarget_journal_path(runtime_state_root: str | Path, run_id: str) -> Path:
    return (
        Path(runtime_state_root)
        / "locks"
        / f"legacy_integration_retarget.{run_id}.json"
    )


def _legacy_retarget_journal_payload(
    run_id: str, from_sha: str, to_sha: str
) -> dict[str, str]:
    return {
        "from_sha": st.require_git_sha(from_sha, name="from_sha"),
        "run_id": run_id,
        "to_sha": st.require_git_sha(to_sha, name="to_sha"),
    }


def _write_legacy_retarget_journal(
    row: Mapping[str, Any], from_sha: str, to_sha: str
) -> Path:
    path = legacy_retarget_journal_path(row["runtime_state_root"], row["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(
        st.canonical_bytes(
            _legacy_retarget_journal_payload(row["run_id"], from_sha, to_sha)
        )
    )
    os.replace(tmp, path)
    return path


def _read_legacy_retarget_journal(row: Mapping[str, Any]) -> Mapping[str, str] | None:
    path = legacy_retarget_journal_path(row["runtime_state_root"], row["run_id"])
    if not path.is_file():
        return None
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, Mapping):
        raise FactoryRefused("LEGACY_INTEGRATION_RETARGET_JOURNAL_INVALID")
    return _legacy_retarget_journal_payload(
        str(payload.get("run_id") or ""),
        str(payload.get("from_sha") or ""),
        str(payload.get("to_sha") or ""),
    )


def _clear_legacy_retarget_journal(row: Mapping[str, Any]) -> None:
    path = legacy_retarget_journal_path(row["runtime_state_root"], row["run_id"])
    if path.is_file():
        path.unlink()


def _finish_legacy_integration_retarget(
    *,
    store: ArtifactStore,
    target: gitpub.TargetBinding,
    run_id: str,
    from_sha: str,
    to_sha: str,
) -> gitpub.TargetBinding:
    source = st.require_git_sha(from_sha, name="from_sha")
    dest = st.require_git_sha(to_sha, name="to_sha")
    current = target.git().read_ref(st.integration_ref(run_id))
    if current is not None and current not in (source, dest):
        raise gitpub.GitPublicationRefused(
            "INTEGRATION_REF_COLLISION", f"{current}!={source}"
        )
    if current != dest:
        gitpub.retarget_integration_ref(target, run_id, source, dest)
    db_sha = str(run_row(store, run_id)["integration_initial_sha"])
    if db_sha not in (source, dest):
        raise FactoryRefused(f"{run_id}:integration_initial_sha")
    if db_sha != dest:
        store.retarget_integration_initial_sha(run_id, source, dest)
    return gitpub.restore_integration_sha(target, dest)


def recover_legacy_retarget_journal(
    *,
    store: ArtifactStore,
    target: gitpub.TargetBinding,
    run_id: str,
) -> gitpub.TargetBinding:
    row = run_row(store, run_id)
    journal = _read_legacy_retarget_journal(row)
    if journal is None:
        return target
    if journal["run_id"] != run_id:
        raise FactoryRefused("LEGACY_INTEGRATION_RETARGET_JOURNAL_MISMATCH")
    updated = _finish_legacy_integration_retarget(
        store=store,
        target=target,
        run_id=run_id,
        from_sha=journal["from_sha"],
        to_sha=journal["to_sha"],
    )
    _clear_legacy_retarget_journal(row)
    return updated


def correct_legacy_integration_base(
    *,
    store: ArtifactStore,
    target: gitpub.TargetBinding,
    run_id: str,
    declared_sha: str,
) -> gitpub.TargetBinding:
    row = run_row(store, run_id)
    stored = str(row["integration_initial_sha"])
    declared = st.require_git_sha(declared_sha, name="declared_sha")
    stages = tuple(
        store.lane_stage(run_id, lane.lane_id)
        for lane in store.active_projection(run_id)
    )
    kinds = recorded_artifact_kinds(store, run_id)
    journal = _read_legacy_retarget_journal(row)
    if journal is not None:
        if journal["run_id"] != run_id or journal["to_sha"] != declared:
            raise FactoryRefused("LEGACY_INTEGRATION_RETARGET_JOURNAL_MISMATCH")
        return recover_legacy_retarget_journal(
            store=store, target=target, run_id=run_id
        )
    action = legacy_integration_correction_decision(
        stored_sha=stored,
        declared_sha=declared,
        lane_stages=stages,
        artifact_kinds=kinds,
    )
    if action == "refuse":
        raise FactoryRefused("LEGACY_INTEGRATION_REBASE_UNSAFE")
    if action in ("noop", "defer"):
        return target
    _write_legacy_retarget_journal(row, stored, declared)
    updated = _finish_legacy_integration_retarget(
        store=store,
        target=target,
        run_id=run_id,
        from_sha=stored,
        to_sha=declared,
    )
    _clear_legacy_retarget_journal(row)
    return updated




def pin_target_from_plan(
    target: gitpub.TargetBinding, compiled: st.CompiledPlan
) -> gitpub.TargetBinding:
    ref = gitpub.declared_integration_ref(gitpub.lane_specs_from_plan(compiled))
    return gitpub.pin_integration_sha(target, ref)


def plan_artifact_ref_for(store: ArtifactStore, run_id: str, plan_revision: int) -> str:
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
        plan_artifact_ref=plan_artifact_ref_for(store, run_id, row["plan_revision"]),
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
    draft_correction: Sequence[Mapping[str, str]] | None = None


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
    def complete_run_spaces(self, run_id: str) -> None: ...



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
    next_stage = st.next_stage_for(
        ctx.stage, artifact.kind, artifact.verdict, lane_kind=ctx.lane.lane_kind
    )
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


def _lane_gate(actor: object, lane_id: str) -> SimpleNamespace | None:
    specs = getattr(actor, "lane_specs", None)
    if not isinstance(specs, Mapping):
        return None
    spec = specs.get(lane_id)
    if not isinstance(spec, Mapping) or "gate" not in spec:
        return None
    gate = spec["gate"]
    if not isinstance(gate, Mapping):
        raise DraftCollectionRefused("lane gate is not a mapping")
    runner = gate.get("runner")
    if runner not in rr.COLLECT_ARGS:
        raise DraftCollectionRefused("unsupported runner")
    min_cases = gate.get("min_cases")
    if isinstance(min_cases, bool) or not isinstance(min_cases, int) or min_cases < 1:
        raise DraftCollectionRefused("min_cases")
    argv = gate.get("argv") or ()
    if not isinstance(argv, (list, tuple)):
        raise DraftCollectionRefused("argv")
    cwd = gate.get("cwd") or "."
    if not isinstance(cwd, str) or not cwd:
        raise DraftCollectionRefused("cwd")
    return SimpleNamespace(
        runner=str(runner),
        argv=tuple(str(item) for item in argv),
        cwd=cwd,
        min_cases=int(min_cases),
    )


def _collect_gate(gate: SimpleNamespace, files: Mapping[str, str]) -> SimpleNamespace:
    written = tuple(sorted({prv.normalize_repo_path(path) for path in files}))
    flags = tuple(token for token in gate.argv if str(token).startswith("-"))
    planned = tuple(
        prv.normalize_repo_path(str(token))
        for token in gate.argv
        if not str(token).startswith("-")
    )
    selectors = planned if planned and set(planned) <= set(written) else written
    return SimpleNamespace(
        runner=gate.runner,
        argv=flags + selectors,
        cwd=gate.cwd,
        min_cases=gate.min_cases,
    )


def _draft_min_cases_findings(
    collected: int, min_cases: int
) -> tuple[dict[str, str], ...]:
    return st.require_revise_findings(
        (
            {
                "implementation_area": "private tests",
                "observed_behavior": "native collect listed {0} cases".format(
                    collected
                ),
                "required_behavior": "native collect must list at least {0} cases".format(
                    min_cases
                ),
                "violated_requirement": "gate.min_cases",
            },
        )
    )


def _remove_collect_tree(dest: Path, vault: Path) -> None:
    dest = Path(dest)
    subprocess.run(
        ["git", "-C", str(vault), "worktree", "remove", "--force", str(dest)],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(vault), "worktree", "prune"],
        capture_output=True,
        check=False,
    )
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)


def _write_test_files(extra: Mapping[str, Any]) -> dict[str, str]:
    files = extra.get("files") or extra.get("private_files") or {}
    if not files:
        raise FactoryRefused("write_tests produced no private files")
    return dict(files)



class FactoryScheduler:
    """Advance every ready lane one frozen stage at a time."""

    def __init__(
        self,
        store: ArtifactStore,
        run_id: str,
        actor: StageActor,
        runtime: RuntimeStateRoot,
        target: gitpub.TargetBinding,
        *,
        stage_started: Optional[Callable[[str, st.LaneStage], None]] = None,
        stage_completed: Optional[
            Callable[[str, st.LaneStage, st.LaneStage], None]
        ] = None,
        compiled: Optional[st.CompiledPlan] = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.actor = actor
        self.runtime = runtime
        self.target = target
        self.stage_started = stage_started
        self.stage_completed = stage_completed
        self._compiled = compiled
        self._compiled_kinds = {
            lane.lane_id: lane.lane_kind for lane in compiled.lanes
        } if compiled is not None else {}
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
            self._recover_legacy_retarget_journal()
            self._resume_orphaned_integration_merge()
            self._maybe_correct_legacy_integration_base()
            ensure_run_integration_ref(self.target, self.store, self.run_id)
            while True:
                status = self.status()
                if status is st.RunStatus.COMPLETE:
                    self._complete_run_spaces()
                    return status
                if status is st.RunStatus.WAITING:
                    return status
                if status is st.RunStatus.PUBLISHABLE:
                    self._publish()
                    if self.status() is st.RunStatus.COMPLETE:
                        self._complete_run_spaces()
                        return st.RunStatus.COMPLETE
                    continue
                if status is st.RunStatus.INTEGRATION_REVIEW_PENDING:
                    self._final_review()
                    continue
                progressed = False
                for lane_id in self.store.ready_lane_ids(self.run_id):
                    stage = self.store.lane_stage(self.run_id, lane_id)
                    if stage is st.LaneStage.READY_TO_MERGE:
                        continue
                    if (
                        stage is st.LaneStage.WRITING_TESTS
                        and self._legacy_correction_action() == "defer"
                    ):
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


    def _recover_legacy_retarget_journal(self) -> None:
        self.locks.acquire(2)
        try:
            self.target = recover_legacy_retarget_journal(
                store=self.store, target=self.target, run_id=self.run_id
            )
        finally:
            self.locks.release()

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
            _review, _invalidation, review_id, invalidation_id = (
                _writing_tests_predecessors(self.store, self.run_id, lane_id)
            )
            return (
                st.writing_tests_input_digest(
                    **common,
                    lane_plan_id=plan.artifact_id,
                    test_review_id=review_id,
                    integration_head=self._integration_head(),
                    test_invalidation_id=invalidation_id,
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
            sealed = self._sealed_for(lane)
            if plan is None:
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
            receipts = self._dep_receipts(lane.needs)
            ids = [plan.artifact_id, sealed.artifact_id]
            ids.extend(
                item.artifact_id for item in receipts if item.artifact_id not in ids
            )
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
            sealed = self._sealed_for(lane)
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
        if stage is st.LaneStage.WRITING_TESTS:
            self._maybe_correct_legacy_integration_base()
        digest, observed = self._pause_input(lane_id, stage)
        self._inflight = (lane_id, stage, digest, observed)
        if self.stage_started is not None:
            self.stage_started(lane_id, stage)
        try:
            if stage is st.LaneStage.READY_TO_MERGE:
                self.locks.acquire(2)
                try:
                    self._ready_to_merge(lane_id)
                finally:
                    self.locks.release()
            elif stage is st.LaneStage.BUILDING:
                self.locks.acquire(2)
                try:
                    self._building(lane_id)
                finally:
                    self.locks.release()
            else:
                dispatch = {
                    st.LaneStage.PLANNED: self._planned,
                    st.LaneStage.WRITING_TESTS: self._writing_tests,
                    st.LaneStage.REVIEWING_TESTS: self._reviewing_tests,
                    st.LaneStage.TESTS_SEALED: self._tests_sealed,
                    st.LaneStage.REVIEWING_CODE: self._reviewing_code,
                }
                dispatch[stage](lane_id)
            if self.stage_completed is not None:
                self.stage_completed(
                    lane_id, stage, self.store.lane_stage(self.run_id, lane_id)
                )
        finally:
            self._inflight = None

    def _common(self, lane_id: str) -> tuple[Mapping[str, Any], st.LaneProjection]:
        row = run_row(self.store, self.run_id)
        if self._compiled is not None:
            projection = next(
                lane for lane in self._compiled.lanes if lane.lane_id == lane_id
            )
        else:
            projection = next(
                lane
                for lane in self.store.active_projection(self.run_id)
                if lane.lane_id == lane_id
            )
        return row, projection

    def _lane_kind(self, lane_id: str) -> str | None:
        if lane_id in self._compiled_kinds:
            return self._compiled_kinds[lane_id]
        return self.store._lane_kind(self.run_id, lane_id)

    def _sealed_suite_gate(self, lane: st.LaneProjection) -> SimpleNamespace | None:
        if lane.lane_kind == st.LANE_KIND_BUILD:
            for dep in lane.needs:
                if self._lane_kind(dep) == st.LANE_KIND_TESTS:
                    return _lane_gate(self.actor, dep)
            return None
        return _lane_gate(self.actor, lane.lane_id)



    def _current_tests_sealed(self, lane_id: str) -> ArtifactRecord | None:
        revision = run_row(self.store, self.run_id)["plan_revision"]
        return _latest(
            self.store,
            self.run_id,
            lane_id,
            st.ArtifactKind.SEALED_TEST_BUNDLE,
            plan_revision=revision,
        )


    def _sealed_for(self, lane: st.LaneProjection) -> ArtifactRecord:
        if lane.lane_kind == st.LANE_KIND_BUILD:
            for dep in lane.needs:
                if self._lane_kind(dep) == st.LANE_KIND_TESTS:
                    sealed = self._current_tests_sealed(dep)
                    if sealed is None:
                        raise FactoryRefused(f"missing dependency sealed tests {dep}")
                    return sealed
            raise FactoryRefused("missing tests-lane sealed bundle")
        sealed = self._current_tests_sealed(lane.lane_id)
        if sealed is None:
            raise FactoryRefused("missing BUILDING inputs")
        return sealed

    def _dep_receipts(self, needs: Sequence[str]) -> list[ArtifactRecord]:
        receipts = []
        revision = run_row(self.store, self.run_id)["plan_revision"]
        for dep in needs:
            if self._lane_kind(dep) == st.LANE_KIND_TESTS:
                sealed = self._current_tests_sealed(dep)
                if sealed is None:
                    raise FactoryRefused(f"missing dependency sealed tests {dep}")
                receipts.append(sealed)
                continue
            merge = _latest(
                self.store,
                self.run_id,
                dep,
                st.ArtifactKind.INTEGRATION_MERGE,
                plan_revision=revision,
            )
            if merge is None:
                raise FactoryRefused(f"missing dependency merge {dep}")
            receipts.append(merge)
        return receipts




    def _plan_artifact_ref(self, row: Mapping[str, Any]) -> str:
        return plan_artifact_ref_for(self.store, self.run_id, row["plan_revision"])

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
        if lane.lane_kind is not None:
            payload["lane_kind"] = lane.lane_kind
        artifact = prv.make_lane_artifact(
            kind=st.ArtifactKind.LANE_PLAN,
            request=_request(ctx),
            payload=payload,
            artifact_ref=f"lane-plan:{self.run_id}:{lane_id}:{digest}",
        )
        _complete(self.store, ctx, artifact)

    def _legacy_correction_action(self) -> str:
        specs = getattr(self.actor, "lane_specs", None)
        if not specs:
            return "noop"
        ref = gitpub.declared_integration_ref(specs)
        declared = self.target.git().rev_parse(ref)
        row = run_row(self.store, self.run_id)
        stages = tuple(
            self.store.lane_stage(self.run_id, lane.lane_id)
            for lane in self.store.active_projection(self.run_id)
        )
        return legacy_integration_correction_decision(
            stored_sha=str(row["integration_initial_sha"]),
            declared_sha=declared,
            lane_stages=stages,
            artifact_kinds=recorded_artifact_kinds(self.store, self.run_id),
        )

    def _maybe_correct_legacy_integration_base(self) -> None:
        specs = getattr(self.actor, "lane_specs", None)
        if not specs:
            return
        ref = gitpub.declared_integration_ref(specs)
        declared = self.target.git().rev_parse(ref)
        self.locks.acquire(2)
        try:
            self.target = correct_legacy_integration_base(
                store=self.store,
                target=self.target,
                run_id=self.run_id,
                declared_sha=declared,
            )
        finally:
            self.locks.release()

    def _writing_tests(self, lane_id: str) -> None:
        row, lane = self._common(lane_id)
        plan = _latest(self.store, self.run_id, lane_id, st.ArtifactKind.LANE_PLAN)
        if plan is None:
            raise FactoryRefused("missing LANE_PLAN")
        review, invalidation, review_id, invalidation_id = _writing_tests_predecessors(
            self.store, self.run_id, lane_id
        )
        tip = self._integration_head()
        digest = st.writing_tests_input_digest(
            run_id=self.run_id,
            lane_id=lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            lane_plan_id=plan.artifact_id,
            test_review_id=review_id,
            integration_head=tip,
            test_invalidation_id=invalidation_id,
        )
        artifacts: dict[str, ArtifactRecord] = {"LANE_PLAN": plan}
        if review is not None:
            artifacts["TEST_REVIEW"] = review
        if invalidation is not None:
            artifacts["TEST_INVALIDATION"] = invalidation
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.WRITING_TESTS,
            artifacts=artifacts,
            integration_head=tip,
        )
        extra = dict(self.actor.write_tests(ctx))
        files = self._require_draft_min_cases(ctx, _write_test_files(extra))
        self._require_typed_test_outputs(lane, files)
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
            _with_input_artifact_ids(
                artifact, [plan.artifact_id, review_id, invalidation_id]
            ),
        )

    def _require_typed_test_outputs(
        self, lane: st.LaneProjection, files: Mapping[str, str]
    ) -> None:
        if lane.lane_kind != st.LANE_KIND_TESTS:
            return
        returned = {prv.normalize_repo_path(path) for path in files}
        declared = set(lane.declared_outputs)
        if returned != declared:
            raise TypedTestOutputsRefused(
                "private files must equal declared outputs"
            )


    def _require_draft_min_cases(
        self, ctx: LaneContext, files: Mapping[str, str]
    ) -> dict[str, str]:
        gate = _lane_gate(self.actor, ctx.lane.lane_id)
        if gate is None:
            return dict(files)
        current = dict(files)
        seen = st.digest_canonical(current)
        collected = self._collect_private_draft(ctx, gate, current)
        if collected >= gate.min_cases:
            return current
        correction = _draft_min_cases_findings(collected, gate.min_cases)
        extra = dict(
            self.actor.write_tests(replace(ctx, draft_correction=correction))
        )
        current = _write_test_files(extra)
        if st.digest_canonical(current) == seen:
            raise DraftMinCasesRefused(collected, gate.min_cases)
        collected = self._collect_private_draft(ctx, gate, current)
        if collected >= gate.min_cases:
            return current
        raise DraftMinCasesRefused(collected, gate.min_cases)

    def _collect_private_draft(
        self,
        ctx: LaneContext,
        gate: SimpleNamespace,
        files: Mapping[str, str],
    ) -> int:
        run_repo = Path(self.target.target_repository_root)
        vault = hv.ensure_vault(self.runtime.path, ctx.run_id)
        base = hv.seed(vault, run_repo, st.integration_ref(self.run_id))
        dest = self.runtime.path / "worktrees" / "draft-collect-{0}-{1}-{2}".format(
            ctx.lane.lane_id, ctx.input_digest[:12], os.urandom(4).hex()
        )
        hv.checkout_vault_worktree(vault, base, dest)
        try:
            prv.write_files(dest, files)
            resolved = rr.resolve(gate.runner, run_repo, gate.cwd)
            ids = rr.collect_cases(
                resolved,
                _collect_gate(gate, files),
                dest,
                runtime_root=run_repo,
            )
            return len(ids)
        except (rr.CollectFailed, rr.RunnerUnusable) as extra:
            detail = getattr(extra, "detail", None) or extra.__class__.__name__
            tokens = prv.collect_private_tokens(
                files=files,
                extra=(str(dest), str(Path(dest).resolve())),
                vault_path=vault,
            )
            raise DraftCollectionRefused(prv.redact_text(str(detail), tokens)) from extra
        finally:
            _remove_collect_tree(dest, vault)



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
        sealed = self._sealed_for(lane)
        if plan is None:
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
        receipts = self._dep_receipts(lane.needs)
        ids = [plan.artifact_id, sealed.artifact_id]
        ids.extend(
            item.artifact_id for item in receipts if item.artifact_id not in ids
        )
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
        artifacts: dict[str, ArtifactRecord] = {
            "LANE_PLAN": plan,
            "SEALED_TEST_BUNDLE": sealed,
        }
        if entry is st.BuildingEntryKind.CODE_REVISE and revise is not None:
            artifacts["CODE_REVIEW"] = revise
        ctx = LaneContext(
            run_id=self.run_id,
            lane=lane,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            plan_artifact_ref=self._plan_artifact_ref(row),
            input_digest=digest,
            stage=st.LaneStage.BUILDING,
            artifacts=artifacts,
            builder_base_sha=builder_base,
            entry_kind=entry,
            public_contract=prv.public_contract(
                acceptance_criteria=lane.public_acceptance,
                declared_outputs=lane.declared_outputs,
            ),
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
        sealed = self._sealed_for(lane)
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
        product_contract = prv.public_contract(
            acceptance_criteria=lane.public_acceptance,
            declared_outputs=lane.declared_outputs,
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
            public_contract=product_contract,
            sealed_digest=str(sealed.payload.get("sealed_digest") or ""),
        )
        ids = [plan.artifact_id, sealed.artifact_id, builder.artifact_id]
        sealed_artifact = _record_as_lane_artifact(sealed, lane)
        request = _request(ctx)
        typed_build = lane.lane_kind == st.LANE_KIND_BUILD
        if not typed_build:
            try:
                cr.detect_candidate_private_collisions(
                    request=request,
                    state_root=self.runtime.path,
                    candidate_repo=Path(self.target.target_repository_root),
                    candidate_sha=builder.payload["candidate_sha"],
                    sealed_bundle=sealed_artifact,
                    scratch_root=self.runtime.path / "worktrees",
                )
            except prv.PrivatePathCollisionError as exc:
                self._complete_test_invalidation(
                    ctx, ids, sealed_artifact, exc
                )
                return
        verdict, findings = self.actor.review_code(ctx)
        constraints = tuple(lane.public_acceptance) or ("produce declared outputs",)
        try:
            artifact = cr.review_builder_output(
                request=request,
                state_root=self.runtime.path,
                candidate_repo=Path(self.target.target_repository_root),
                candidate_sha=builder.payload["candidate_sha"],
                candidate_ref=builder.payload["candidate_ref"],
                builder_base_sha=builder.payload["builder_base_sha"],
                sealed_bundle=sealed_artifact,
                verdict=verdict,
                findings=findings,
                scratch_root=self.runtime.path / "worktrees",
                architecture_constraints=constraints,
                allow_candidate_paths=typed_build,
                public_contract=product_contract,
                gate=self._sealed_suite_gate(lane),
                runtime_root=Path(self.target.target_repository_root),
            )
        except prv.PrivatePathCollisionError as exc:
            self._complete_test_invalidation(
                ctx, ids, sealed_artifact, exc
            )
            return
        _complete(
            self.store,
            ctx,
            _with_input_artifact_ids(artifact, ids),
        )

    def _complete_test_invalidation(
        self,
        ctx: LaneContext,
        ids: Sequence[str],
        sealed_bundle: st.LaneArtifact,
        collision: prv.PrivatePathCollisionError,
    ) -> None:
        vault = hv.ensure_vault(self.runtime.path, ctx.run_id)
        files = tc.sealed_private_files(vault, sealed_bundle)
        private_files = {
            path: hv.cat_blob(vault, blob).decode("utf-8")
            for path, blob in files.items()
        }
        tokens = prv.collect_private_tokens(
            files=private_files,
            vault_path=vault,
            vault_refs=(sealed_bundle.artifact_ref,),
            blob_ids=tuple(files.values()),
        )
        allow = prv.public_contract_allow(
            sealed_bundle.payload["public_contract"],
            extra=(
                ctx.input_digest,
                str(sealed_bundle.payload.get("sealed_digest") or ""),
            ),
        )
        payload = cr.test_invalidation_payload(
            input_digest=ctx.input_digest,
            input_artifact_ids=ids,
            collision=collision,
            tokens=tokens,
            allow=allow,
        )
        artifact = prv.make_lane_artifact(
            kind=st.ArtifactKind.TEST_INVALIDATION,
            request=_request(ctx),
            payload=payload,
            artifact_ref="test-invalidation:{0}".format(ctx.input_digest),
        )
        _complete(self.store, ctx, _with_input_artifact_ids(artifact, ids))



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
                lane=self._owner_lane(lanes),
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

    def _owner_lane(
        self, lanes: Sequence[st.LaneProjection]
    ) -> st.LaneProjection:
        if self._compiled is not None:
            order = self._compiled.integration_order
        else:
            order = st.topological_integration_order(lanes)
        if not order:
            raise FactoryRefused("missing owner lane")
        owner_id = order[-1]
        for lane in lanes:
            if lane.lane_id == owner_id:
                return lane
        raise FactoryRefused("missing owner lane {0}".format(owner_id))


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

    def _complete_run_spaces(self) -> None:
        if not _has_publication(self.store, self.run_id):
            return
        try:
            self.actor.complete_run_spaces(self.run_id)
        except Exception as extra:
            if getattr(extra, "code", "") == "CLEANUP_REFUSED":
                raise
            raise FactoryRefused(
                "complete_run_spaces:{0}".format(extra)
            ) from extra



def create_factory_run(
    *,
    store: ArtifactStore,
    run_id: str,
    compiled: st.CompiledPlan,
    runtime: RuntimeStateRoot,
    target: gitpub.TargetBinding,
) -> st.RunBinding:
    runtime.ensure_layout()
    pinned = pin_target_from_plan(target, compiled)
    binding = st.RunBinding(
        runtime_state_root=str(runtime.path),
        runtime_state_fingerprint=runtime.fingerprint,
        integration_ref=st.integration_ref(run_id),
        integration_initial_sha=pinned.integration_initial_sha,
        target_repository_root=pinned.target_repository_root,
        target_git_common_dir=pinned.target_git_common_dir,
        target_worktree_git_dir=pinned.target_worktree_git_dir,
        target_object_format=pinned.target_object_format,
        target_repository_fingerprint=pinned.target_repository_fingerprint,
        target_sync_journal_fingerprint=pinned.target_sync_journal_fingerprint,
        target_initial_main_sha=pinned.target_initial_main_sha,
        target_main_ref=pinned.target_main_ref,
    )
    try:
        store.create_run(run_id, compiled, binding)
    except RunAlreadyExists:
        existing = binding_from_run(run_row(store, run_id))
        if existing != binding:
            raise
    ensure_run_integration_ref(pinned, store, run_id)
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
        target = st.amendment_reset_stage(
            current,
            changed=changed,
            wait_reason=wait,
            lane_kind=lane.lane_kind,
        )
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
                    **(
                        {"lane_kind": lane.lane_kind}
                        if lane.lane_kind is not None
                        else {}
                    ),
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


