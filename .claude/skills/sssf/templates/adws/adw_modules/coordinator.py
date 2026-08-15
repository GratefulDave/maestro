"""Durable, receipt-authorized ADWS workspace coordinator."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import math
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Set, Tuple

from . import coordinator_store as cs
from . import workspace_canonical
from . import workspace_digest
from . import workspace_model as wm
from . import workspace_receipt as wr
from .participant import (ParticipantCancelled, ParticipantContext,
                          ParticipantRunner, load_participant_result)
from .workspace_runtime import (CandidateRepository,
                                GateFailure, GitEnvironmentalError,
                                assemble_acceptance, cleanup_acceptance,
                                prepare_candidate, reclaim_acceptance,
                                repository_binding, resolve_repository_paths,
                                run_global_gates, validated_writer_plan_bytes,
                                verify_accepted)
from .worktree import GateCancelled


class CoordinatorError(RuntimeError):
    """The durable workspace contract cannot be satisfied."""


class LeaseUnavailable(CoordinatorError):
    """A different coordinator has the live lease for this run."""


@dataclass(frozen=True)
class CoordinatorConfig:
    max_workers: int = 1
    lease_owner: str = "workspace-coordinator"
    lease_stale_after_s: float = 30.0
    participant_timeout_s: float = 3600.0
    cancellation_timeout_s: float = 10.0
    poll_interval_s: float = 0.05
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if (not isinstance(self.max_workers, int) or isinstance(self.max_workers, bool)
                or self.max_workers < 1):
            raise ValueError("max_workers must be a positive integer")
        if not isinstance(self.lease_owner, str) or not self.lease_owner:
            raise ValueError("lease_owner must be a nonempty string")
        for name in ("lease_stale_after_s", "participant_timeout_s",
                     "cancellation_timeout_s", "poll_interval_s"):
            value = getattr(self, name)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(float(value)) or value <= 0):
                raise ValueError("{0} must be a positive finite number".format(name))
        if self.cancellation_timeout_s >= self.lease_stale_after_s:
            raise ValueError(
                "cancellation_timeout_s must be less than lease_stale_after_s")
        if self.poll_interval_s >= self.lease_stale_after_s:
            raise ValueError("poll_interval_s must be less than lease_stale_after_s")
        if not callable(self.clock):
            raise ValueError("clock must be callable")


@dataclass(frozen=True)
class _Completion:
    repository_id: str
    state: wm.RepositoryState
    accepted_sha: Optional[str] = None
    reason: Optional[str] = None


class WorkspaceCoordinator:
    """Run a frozen workspace plan, recovering only from store authority."""

    def __init__(self, *, run_id: str, plan: wm.WorkspacePlan,
                 workspace_digest: str, receipt: wr.WorkspaceReceipt,
                 store: cs.CoordinatorStore, manifest_dir: Path,
                 state_root: Path, participant_runner: ParticipantRunner,
                 config: CoordinatorConfig) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a nonempty string")
        if not isinstance(plan, wm.WorkspacePlan):
            raise ValueError("plan must be a WorkspacePlan")
        if not isinstance(workspace_digest, str) or not workspace_digest:
            raise ValueError("workspace_digest must be a nonempty string")
        if not isinstance(receipt, wr.WorkspaceReceipt):
            raise ValueError("receipt must be a WorkspaceReceipt")
        if not isinstance(store, cs.CoordinatorStore):
            raise ValueError("store must be a CoordinatorStore")
        if not isinstance(config, CoordinatorConfig):
            raise ValueError("config must be a CoordinatorConfig")
        if not hasattr(participant_runner, "run") or not hasattr(participant_runner, "cancel"):
            raise ValueError("participant_runner must implement ParticipantRunner")
        self.run_id, self.plan = run_id, plan
        self.workspace_digest, self.receipt = workspace_digest, receipt
        self.store = store
        self.manifest_dir, self.state_root = Path(manifest_dir), Path(state_root).resolve()
        self.participant_runner, self.config = participant_runner, config
        self._lease_token = "{0}:{1}:{2}".format(
            config.lease_owner, os.getpid(), uuid.uuid4().hex)
        self._owned_running = set()
        self._lease_heartbeat_due_at: Optional[float] = None

    def run(self) -> wm.WorkspaceOutcome:
        self._owned_running.clear()
        self._authorize()
        run = self._project_once()
        try:
            paths = resolve_repository_paths(self.manifest_dir, self.plan)
            bindings = {
                repository_id: repository_binding(path)
                for repository_id, path in paths.items()
            }
        except GitEnvironmentalError:
            raise
        except Exception as exc:
            if (run.outcome is not None
                    or any(record.resolved_path is not None
                           for record in self.store.list_repositories(self.run_id))):
                raise cs.RepositoryPathMismatch(
                    "repository paths cannot be resolved against this run's binding"
                ) from exc
            self._acquire_lease()
            try:
                current = self.store.get_run(self.run_id)
                if current.outcome is not None:
                    return current.outcome
                if self.store.cancellation_requested(self.run_id):
                    self._cancel_active(set())
                    records = self.store.list_repositories(self.run_id)
                    outcome = (wm.WorkspaceOutcome.BLOCKED
                               if any(record.state is wm.RepositoryState.BLOCKED
                                      for record in records)
                               else wm.WorkspaceOutcome.CANCELLED)
                    return self._declare(outcome)
                self._block_all_nonterminal(self._reason(exc))
                return self._declare(wm.WorkspaceOutcome.BLOCKED)
            finally:
                self.store.release_lease(self.run_id, self._lease_token)
        self._acquire_lease()
        try:
            run = self.store.get_run(self.run_id)
            self.store.bind_repository_paths(
                self.run_id, bindings, lease_owner=self._lease_token)
            if run.outcome is not None:
                return run.outcome
            self.store.bind_repository_paths(
                self.run_id, bindings, lease_owner=self._lease_token)
            return self._execute(paths)
        finally:
            self.store.release_lease(self.run_id, self._lease_token)

    def _verify_repository_identity(self, record: cs.RepositoryRecord,
                                    path: Path) -> None:
        binding = repository_binding(path)
        if (record.resolved_path != binding.resolved_path
                or record.git_common_dir != binding.git_common_dir
                or record.repository_identity != binding.repository_identity):
            raise cs.RepositoryPathMismatch(
                "repository {0} no longer matches its persisted Git identity".format(
                    record.repository_id))

    def _verify_all_repository_identities(self,
                                          paths: Mapping[str, Path]) -> None:
        self.store.bind_repository_paths(
            self.run_id,
            {repository_id: repository_binding(path)
             for repository_id, path in paths.items()},
            lease_owner=self._lease_token)

    def _authorize(self) -> None:
        canonical_digest = workspace_digest.digest_of(
            workspace_canonical.canonicalize_workspace(self.plan))
        if self.workspace_digest != canonical_digest:
            raise wr.AuthorizationError(
                "workspace digest does not match the canonical workspace plan")
        vector = tuple(wr.ParticipantAuthorization(
            repository_id=spec.repository_id, mode=spec.mode,
            base_commit=spec.base_commit, plan_digest=spec.plan_digest,
            target_branch=spec.target_branch) for spec in self.plan.repositories)
        if not self.receipt.authorizes(self.workspace_digest, vector):
            raise wr.AuthorizationError(
                "workspace receipt does not authorize the exact workspace vector")

    def _project_once(self) -> cs.WorkspaceRunRecord:
        try:
            return self.store.create_run(self.run_id, self.workspace_digest, self.plan)
        except cs.RunAlreadyExists:
            run = self.store.get_run(self.run_id)
            if run.workspace_digest != self.workspace_digest or run.workspace != self.plan:
                raise CoordinatorError("stored run does not match authorized workspace")
            return run

    def _now(self) -> float:
        value = self.config.clock()
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value))):
            raise CoordinatorError("clock returned a non-finite numeric timestamp")
        return float(value)

    def _acquire_lease(self) -> None:
        now = self._now()
        if not self.store.acquire_lease(self.run_id, self._lease_token,
                                        now, self.config.lease_stale_after_s):
            raise LeaseUnavailable("workspace run has a live lease owned by another coordinator")
        self._lease_heartbeat_due_at = (
            now + self.config.lease_stale_after_s / 3.0)

    def _heartbeat(self) -> None:
        now = self._now()
        if (self._lease_heartbeat_due_at is not None
                and now < self._lease_heartbeat_due_at):
            return
        if not self.store.heartbeat_lease(self.run_id, self._lease_token,
                                          now, self.config.lease_stale_after_s):
            raise LeaseUnavailable("workspace run lease was lost while executing")
        self._lease_heartbeat_due_at = (
            now + self.config.lease_stale_after_s / 3.0)

    def _execute(self, paths: Mapping[str, Path]) -> wm.WorkspaceOutcome:
        futures: Dict[Future, cs.RepositoryRecord] = {}
        terminal_future: Optional[Future] = None
        cancelled: Set[str] = set()
        executor = ThreadPoolExecutor(
            max_workers=self.config.max_workers, thread_name_prefix="workspace-child")
        try:
            while True:
                self._heartbeat()
                if terminal_future is not None:
                    if terminal_future.done():
                        return terminal_future.result()
                    wait((terminal_future,), timeout=self.config.poll_interval_s,
                         return_when=FIRST_COMPLETED)
                    continue
                if self.store.cancellation_requested(self.run_id):
                    self._cancel_active(cancelled)
                    self._discard_terminal_futures(futures)
                self._collect(futures)
                self.store.block_pending_descendants(
                    self.run_id, lease_owner=self._lease_token)
                records = self.store.list_repositories(self.run_id)
                if self.store.cancellation_requested(self.run_id):
                    self._cancel_active(cancelled)
                    self._discard_terminal_futures(futures)
                    if not futures:
                        records = self.store.list_repositories(self.run_id)
                        if any(record.state is wm.RepositoryState.BLOCKED
                               for record in records):
                            return self._declare(wm.WorkspaceOutcome.BLOCKED)
                        return self._declare(wm.WorkspaceOutcome.CANCELLED)
                else:
                    active_ids = {record.repository_id for record in futures.values()}
                    ready = tuple(record for record in self._ready(records)
                                  if record.repository_id not in active_ids)
                    if ready:
                        self._start_ready(ready, paths, executor, futures)
                    if not futures:
                        records = self.store.list_repositories(self.run_id)
                        if self._terminal(records):
                            terminal_future = executor.submit(self._finish, paths)
                            continue
                        if not ready:
                            raise CoordinatorError(
                                "no repository is runnable in nonterminal state")
                if futures:
                    wait(tuple(futures), timeout=self.config.poll_interval_s,
                         return_when=FIRST_COMPLETED)
        # BaseException denotes coordinator interruption: retain durable
        # RUNNING claims for recovery instead of declaring child cancellation.
        except Exception:
            self._cancel_active(cancelled)
            self._discard_terminal_futures(futures)
            raise
        finally:
            executor.shutdown(wait=False)

    @staticmethod
    def _terminal(records: Sequence[cs.RepositoryRecord]) -> bool:
        return all(record.state in (wm.RepositoryState.ACCEPTED,
                                    wm.RepositoryState.BLOCKED,
                                    wm.RepositoryState.CANCELLED)
                   for record in records)

    @staticmethod
    def _ready(records: Sequence[cs.RepositoryRecord]) -> Tuple[cs.RepositoryRecord, ...]:
        by_id = {record.repository_id: record for record in records}
        return tuple(record for record in records
                     if record.state in (wm.RepositoryState.PENDING,
                                         wm.RepositoryState.RUNNING)
                     and all(by_id[need].state is wm.RepositoryState.ACCEPTED
                             for need in record.spec.needs))

    def _start_ready(self, ready: Sequence[cs.RepositoryRecord], paths: Mapping[str, Path],
                     executor: ThreadPoolExecutor,
                     futures: Dict[Future, cs.RepositoryRecord]) -> None:
        slots = self.config.max_workers - len(futures)
        for record in ready:
            path = paths[record.repository_id]
            if record.state is wm.RepositoryState.RUNNING:
                if record.spec.mode is wm.RepositoryMode.READ_ONLY:
                    self._resume_read_only(record, path)
                elif slots:
                    # Resume only the deterministic branch/worktree claimed in
                    # durable authority; _run_writer re-proves it before use.
                    self._owned_running.add(record.repository_id)
                    futures[executor.submit(
                        self._run_writer, record, path, True)] = record
                    slots -= 1
            elif record.spec.mode is wm.RepositoryMode.READ_ONLY:
                self._accept_read_only(record, path)
            elif slots:
                claimed = self._claim(record)
                if claimed is not None:
                    futures[executor.submit(self._run_writer, claimed, path, False)] = claimed
                    slots -= 1

    def _claim(self, record: cs.RepositoryRecord) -> Optional[cs.RepositoryRecord]:
        try:
            claimed = self.store.claim_repository(
                self.run_id, record.repository_id, self._child_id(record),
                self._branch(record), lease_owner=self._lease_token)
        except cs.IllegalTransition:
            return None
        self._owned_running.add(claimed.repository_id)
        return claimed

    def _accept_read_only(self, record: cs.RepositoryRecord, path: Path) -> None:
        self._verify_repository_identity(record, path)
        try:
            claimed = self.store.claim_repository(
                self.run_id, record.repository_id, self._child_id(record), None,
                lease_owner=self._lease_token)
        except cs.IllegalTransition:
            return
        if self.store.cancellation_requested(self.run_id):
            self._cancel_record(claimed, "cancellation-requested")
            return
        self.store.transition_repository(
            self.run_id, claimed.repository_id, wm.RepositoryState.ACCEPTED,
            accepted_sha=claimed.spec.base_commit, reason="read-only-base-verified",
            lease_owner=self._lease_token)

    def _resume_read_only(self, record: cs.RepositoryRecord, path: Path) -> None:
        if (record.child_run_id != self._child_id(record)
                or record.candidate_branch is not None):
            self._block(record, "invalid-running-read-only-identity")
            return
        self._verify_repository_identity(record, path)
        if self.store.cancellation_requested(self.run_id):
            self._cancel_record(record, "cancellation-requested")
            return
        self.store.transition_repository(
            self.run_id, record.repository_id, wm.RepositoryState.ACCEPTED,
            accepted_sha=record.spec.base_commit, reason="read-only-base-recovered",
            lease_owner=self._lease_token)

    def _collect(self, futures: Dict[Future, cs.RepositoryRecord]) -> None:
        for future in tuple(future for future in futures if future.done()):
            claimed = futures.pop(future)
            completion = future.result()  # Preserve typed retriable failures.
            self._owned_running.discard(claimed.repository_id)
            current = self.store.get_repository(self.run_id, claimed.repository_id)
            if current.state is not wm.RepositoryState.RUNNING:
                continue
            if self.store.cancellation_requested(self.run_id):
                self._cancel_record(current, "cancellation-requested")
            elif completion.state is wm.RepositoryState.ACCEPTED:
                self.store.transition_repository(
                    self.run_id, current.repository_id, wm.RepositoryState.ACCEPTED,
                    accepted_sha=completion.accepted_sha, reason=completion.reason,
                    lease_owner=self._lease_token)
            elif completion.state is wm.RepositoryState.CANCELLED:
                self._cancel_record(current, completion.reason or "participant-cancelled")
            else:
                self._block_after_participant_failure(
                    current, completion.reason or "participant-blocked")

    def _discard_terminal_futures(
            self, futures: Dict[Future, cs.RepositoryRecord]) -> None:
        """Detach terminal authority from workers that cannot update it directly."""
        for future, record in tuple(futures.items()):
            current = self.store.get_repository(self.run_id, record.repository_id)
            if current.state is wm.RepositoryState.RUNNING:
                continue
            futures.pop(future)
            future.cancel()
            self._owned_running.discard(record.repository_id)

    def _block_after_participant_failure(self, record: cs.RepositoryRecord,
                                         reason: str) -> None:
        quiesced, attempts = self._retry_cancel(record)
        cleanup = ("cleanup-proven" if quiesced else "cleanup-unproven")
        self._block(
            record, "{0}; {1}; cancel-attempts={2}".format(
                reason, cleanup, attempts))

    def _retry_cancel(self, record: cs.RepositoryRecord) -> Tuple[bool, int]:
        """Make at most two bounded cleanup attempts before preserving evidence."""
        deadline = time.monotonic() + float(self.config.cancellation_timeout_s)
        attempts = 0
        while attempts < 2 and time.monotonic() < deadline:
            attempts += 1
            result = self._bounded_cancel(record, deadline)
            if result is True:
                return True, attempts
            if result is None:
                break
        return False, attempts

    def _bounded_cancel(self, record: cs.RepositoryRecord,
                        deadline: float) -> Optional[bool]:
        """Do not let a hostile runner retain coordinator control past deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        completed = threading.Event()
        result = [False]

        def invoke() -> None:
            try:
                result[0] = bool(self.participant_runner.cancel(
                    self.run_id, record.repository_id, deadline))
            except BaseException:
                result[0] = False
            finally:
                completed.set()

        worker = threading.Thread(
            target=invoke, name="workspace-cancel-{0}".format(
                record.repository_id), daemon=True)
        worker.start()
        completed.wait(remaining)
        return result[0] if completed.is_set() else None

    def _block_all_nonterminal(self, reason: str) -> None:
        for record in self.store.list_repositories(self.run_id):
            self._block(record, reason)

    def _cancel_active(self, sent: Set[str]) -> None:
        for record in self.store.list_repositories(self.run_id):
            if record.state is wm.RepositoryState.PENDING:
                self._cancel_record(record, "cancellation-requested")
                continue
            if (record.state is not wm.RepositoryState.RUNNING
                    or record.repository_id in sent):
                continue
            sent.add(record.repository_id)
            if record.repository_id not in self._owned_running:
                self._block(record, "running-participant-liveness-unproven")
                continue
            quiesced, attempts = self._retry_cancel(record)
            self._owned_running.discard(record.repository_id)
            if quiesced:
                self._cancel_record(record, "cancellation-requested")
            else:
                self._block(
                    record,
                    "cancellation-cleanup-unproven; cancel-attempts={0}".format(
                        attempts))
    def _cancel_record(self, record: cs.RepositoryRecord, reason: str) -> None:
        current = self.store.get_repository(self.run_id, record.repository_id)
        if current.state in (wm.RepositoryState.PENDING, wm.RepositoryState.RUNNING):
            self.store.transition_repository(
                self.run_id, current.repository_id, wm.RepositoryState.CANCELLED,
                reason=reason, lease_owner=self._lease_token)

    def _block(self, record: cs.RepositoryRecord, reason: str) -> None:
        current = self.store.get_repository(self.run_id, record.repository_id)
        if current.state in (wm.RepositoryState.PENDING, wm.RepositoryState.RUNNING):
            self.store.transition_repository(
                self.run_id, current.repository_id, wm.RepositoryState.BLOCKED,
                reason=reason, lease_owner=self._lease_token)
            self.store.block_pending_descendants(
                self.run_id, lease_owner=self._lease_token)

    def _child_id(self, record: cs.RepositoryRecord) -> str:
        return "{0}:{1}".format(self.run_id, record.repository_id)

    def _branch(self, record: cs.RepositoryRecord) -> str:
        return "maestro/workspace/{0}/{1}/candidate".format(
            self.run_id, record.repository_id)

    def _result_path(self, record: cs.RepositoryRecord) -> Path:
        return (self.state_root / "participant-results" / self.run_id /
                record.repository_id / "result.json")

    def _resume_candidate(self, record: cs.RepositoryRecord,
                          repository_path: Path) -> CandidateRepository:
        branch = self._branch(record)
        if record.child_run_id != self._child_id(record):
            raise CoordinatorError("RUNNING repository has a non-deterministic child identity")
        if record.candidate_branch != branch:
            raise CoordinatorError("RUNNING repository has a non-deterministic candidate branch")
        resume = CandidateRepository(
            record.repository_id, Path(repository_path).resolve(),
            record.spec.base_commit.lower(), branch,
            self.state_root / "candidates" / self.run_id / record.repository_id / "candidate")
        return prepare_candidate(self.run_id, record.spec, repository_path,
                                 self.state_root, resume=resume)

    def _copy_plan(self, candidate: CandidateRepository, spec: wm.RepositorySpec,
                   plan_bytes: bytes) -> Path:
        if spec.plan_path is None:
            raise CoordinatorError("writer has no plan path")
        target = candidate.candidate_worktree / spec.plan_path
        try:
            root = candidate.candidate_worktree.resolve()
            resolved = target.resolve()
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CoordinatorError(
                "bound plan escapes candidate worktree: {0}".format(exc))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(plan_bytes)
        except OSError as exc:
            raise CoordinatorError("cannot write bound plan into candidate: {0}".format(exc))
        return resolved

    def _run_writer(self, record: cs.RepositoryRecord, path: Path,
                    resuming: bool) -> _Completion:
        try:
            self._verify_repository_identity(record, path)
            candidate = (self._resume_candidate(record, path) if resuming
                         else prepare_candidate(self.run_id, record.spec, path, self.state_root))
            if candidate.candidate_branch != record.candidate_branch:
                raise CoordinatorError("candidate branch does not match durable claim")
            result_path = self._result_path(record)
            if result_path.exists() or result_path.is_symlink():
                if not resuming:
                    raise CoordinatorError(
                        "fresh participant claim refuses a pre-existing result")
                result = load_participant_result(result_path, self._child_id(record))
            else:
                plan_bytes = validated_writer_plan_bytes(path, record.spec)
                plan_path = self._copy_plan(candidate, record.spec, plan_bytes)
                result_path.parent.mkdir(parents=True, exist_ok=True)
                if self.store.cancellation_requested(self.run_id):
                    return _Completion(record.repository_id,
                                       wm.RepositoryState.CANCELLED,
                                       reason="cancellation-requested")
                plan_digest = record.spec.plan_digest
                if plan_digest is None:
                    raise CoordinatorError("writer has no plan digest")
                result = self.participant_runner.run(ParticipantContext(
                    workspace_run_id=self.run_id, repository_id=record.repository_id,
                    child_run_id=self._child_id(record), plan_path=plan_path,
                    plan_digest=plan_digest,
                    candidate_branch=candidate.candidate_branch,
                    candidate_worktree=candidate.candidate_worktree,
                    participant_result_path=result_path, run_argv=record.spec.run_argv),
                    timeout=float(self.config.participant_timeout_s))
            outcome, reason = getattr(result, "outcome", None), getattr(result, "reason", None)
            if outcome == "accepted":
                return _Completion(record.repository_id, wm.RepositoryState.ACCEPTED,
                                   verify_accepted(candidate, result), "participant-accepted")
            if outcome == "cancelled":
                return _Completion(record.repository_id, wm.RepositoryState.CANCELLED,
                                   reason=reason or "participant-cancelled")
            if outcome == "blocked":
                return _Completion(record.repository_id, wm.RepositoryState.BLOCKED,
                                   reason=reason or "participant-blocked")
            raise CoordinatorError("participant returned an unknown outcome")
        except ParticipantCancelled as exc:
            state = (wm.RepositoryState.CANCELLED if self.store.cancellation_requested(self.run_id)
                     else wm.RepositoryState.BLOCKED)
            return _Completion(record.repository_id, state, reason=self._reason(exc))
        except GitEnvironmentalError:
            raise
        except Exception as exc:
            return _Completion(record.repository_id, wm.RepositoryState.BLOCKED,
                               reason=self._reason(exc))

    @staticmethod
    def _reason(exc: Exception) -> str:
        return "{0}: {1}".format(type(exc).__name__, str(exc).strip() or "no detail")

    def _finish(self, paths: Mapping[str, Path]) -> wm.WorkspaceOutcome:
        records = self.store.list_repositories(self.run_id)
        if any(record.state is wm.RepositoryState.BLOCKED for record in records):
            return self._declare(wm.WorkspaceOutcome.BLOCKED)
        if self.store.cancellation_requested(self.run_id):
            return self._declare(wm.WorkspaceOutcome.CANCELLED)
        if any(record.state is wm.RepositoryState.CANCELLED for record in records):
            return self._declare(wm.WorkspaceOutcome.CANCELLED)
        return self._gates(paths, records)

    def _gates(self, paths: Mapping[str, Path], records: Sequence[cs.RepositoryRecord]
              ) -> wm.WorkspaceOutcome:
        self._verify_all_repository_identities(paths)
        reclaim_acceptance(self.run_id, self.plan, paths, self.state_root)
        stored = {gate.gate_index: gate for gate in self.store.list_gates(self.run_id)}
        if any(not gate.passed for gate in stored.values()):
            return self._declare(wm.WorkspaceOutcome.BLOCKED)
        if self.store.cancellation_requested(self.run_id):
            return self._declare(wm.WorkspaceOutcome.CANCELLED)
        pending = tuple(index for index in range(len(self.plan.integration_gates))
                        if index not in stored)
        if not pending:
            return self._declare(wm.WorkspaceOutcome.ACCEPTED)

        accepted: Dict[str, str] = {}
        for record in records:
            if record.spec.mode is not wm.RepositoryMode.WRITE:
                continue
            if record.accepted_sha is None:
                raise CoordinatorError(
                    "accepted writer has no accepted commit: {0}".format(
                        record.repository_id))
            accepted[record.repository_id] = record.accepted_sha
        try:
            acceptance = assemble_acceptance(
                self.run_id, self.plan, paths, accepted, self.state_root)
        except GitEnvironmentalError:
            reclaim_acceptance(self.run_id, self.plan, paths, self.state_root)
            raise
        except Exception as exc:
            # A failed assembly may have registered only a subset of its
            # deterministic worktrees.  Reclaim must finish before this error
            # is made durable as a failed gate.
            reclaim_acceptance(self.run_id, self.plan, paths, self.state_root)
            self.store.record_gate(
                self.run_id, pending[0], passed=False,
                detail={"error": self._reason(exc)}, lease_owner=self._lease_token)
            return self._declare(wm.WorkspaceOutcome.BLOCKED)

        desired = wm.WorkspaceOutcome.ACCEPTED
        try:
            for index in pending:
                if self.store.cancellation_requested(self.run_id):
                    desired = wm.WorkspaceOutcome.CANCELLED
                    break
                try:
                    results = run_global_gates(
                        acceptance, (self.plan.integration_gates[index],),
                        cancel_requested=lambda: self.store.cancellation_requested(
                            self.run_id))
                    if len(results) != 1:
                        raise CoordinatorError(
                            "one declared global gate must yield one result")
                    self.store.record_gate(
                        self.run_id, index, passed=True,
                        detail=self._gate_detail(results[0]),
                        lease_owner=self._lease_token)
                except GateCancelled:
                    desired = wm.WorkspaceOutcome.CANCELLED
                    break
                except GateFailure as failure:
                    self.store.record_gate(
                        self.run_id, index, passed=False,
                        detail=self._gate_detail(failure.result),
                        lease_owner=self._lease_token)
                    desired = wm.WorkspaceOutcome.BLOCKED
                    break
                except Exception as exc:
                    self.store.record_gate(
                        self.run_id, index, passed=False,
                        detail={"error": self._reason(exc)},
                        lease_owner=self._lease_token)
                    desired = wm.WorkspaceOutcome.BLOCKED
                    break
                if self.store.cancellation_requested(self.run_id):
                    desired = wm.WorkspaceOutcome.CANCELLED
                    break
        finally:
            try:
                cleanup_acceptance(acceptance)
            except Exception:
                reclaim_acceptance(self.run_id, self.plan, paths, self.state_root)
                raise
        return self._declare(desired)

    @staticmethod
    def _gate_detail(result: Any) -> Mapping[str, Any]:
        return {"label": getattr(result, "label", ""),
                "scope": getattr(result, "scope", ""),
                "selector": getattr(result, "selector", None),
                "command": list(getattr(result, "command", ())),
                "exit_code": getattr(result, "exit_code", None),
                "green": bool(getattr(result, "green", False)),
                "counts": dict(getattr(result, "counts", {})),
                "tail": list(getattr(result, "tail", ()))}

    def _declare(self, outcome: wm.WorkspaceOutcome) -> wm.WorkspaceOutcome:
        run = self.store.declare_outcome(
            self.run_id, outcome, lease_owner=self._lease_token)
        if run.outcome is None:
            raise CoordinatorError("outcome declaration returned no outcome")
        return run.outcome
