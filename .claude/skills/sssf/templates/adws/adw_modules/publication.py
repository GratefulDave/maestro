"""Durable, compare-and-swap publication of an accepted workspace run.

Publication authority lives in :mod:`coordinator_store`.  This module performs
only the external Git/GitHub operations represented by that authority and
records every completed operation through the store's public API.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from . import coordinator_store as cs
from . import workspace_model as wm
from . import workspace_runtime as workspace_runtime


CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess]

_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_PUBLICATION_LOCKS = {}

_PUBLICATION_LEASE_STALE_AFTER_S = 30.0


def _process_publication_lock(db_path: str):
    """Return the one in-process lock for the canonical store database path."""
    key = str(Path(db_path).resolve())
    with _PROCESS_LOCK_GUARD:
        lock = _PROCESS_PUBLICATION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_PUBLICATION_LOCKS[key] = lock
        return lock


class PublicationError(RuntimeError):
    """A ref check or external publication command refused publication."""



class PublicationEnvironmentalError(PublicationError):
    """Git or filesystem infrastructure failed; no durable conflict is inferred."""

@dataclass(frozen=True)
class PublicationResult:
    """The durable state and workspace outcome after one publication action."""

    run_id: str
    outcome: wm.WorkspaceOutcome
    intent: cs.PublicationIntentRecord
    reason: Optional[str] = None
    steps: Tuple[cs.PublicationStepRecord, ...] = ()


def _subprocess_runner(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run one argv-only publication command in its explicitly supplied repo."""
    return subprocess.run(
        tuple(argv), cwd=str(cwd), capture_output=True, text=True, shell=False,
    )


def _same_sha(left: str, right: str) -> bool:
    return left.lower() == right.lower()


def _command_message(result: subprocess.CompletedProcess) -> str:
    output = (result.stderr or result.stdout or "").strip()
    return output[-400:] if output else "exit {0}".format(result.returncode)


class WorkspacePublisher:
    """Publish a persisted target vector without reconstructing its authority.

    ``repository_paths`` is deliberately an explicit mapping produced by the
    workspace runtime.  Publication never derives a repository location from a
    process working directory or from mutable audit data.
    """

    def __init__(self, *, store: cs.CoordinatorStore,
                 repository_paths: Mapping[str, Path],
                 command_runner: Optional[CommandRunner] = None,
                 actor: str = "coordinator") -> None:
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        paths: Dict[str, Path] = {}
        for repository_id, path in repository_paths.items():
            if not isinstance(repository_id, str) or not repository_id:
                raise ValueError("repository_paths keys must be nonempty strings")
            repository_path = Path(path)
            if not repository_path.is_absolute():
                raise ValueError(
                    "repository path for {0} must be absolute".format(repository_id))
            canonical_path = repository_path.resolve()
            if any(canonical_path == other
                   or canonical_path in other.parents
                   or other in canonical_path.parents
                   for other in paths.values()):
                raise ValueError(
                    "repository paths must be distinct and non-overlapping")
            paths[repository_id] = canonical_path
        self._store = store
        self._repository_paths = paths
        self._command_runner = command_runner or _subprocess_runner
        self._actor = actor
        self._active_lease_run_id: Optional[str] = None

    def _require_persisted_repository_paths(self, run_id: str) -> None:
        """Refuse any publication vector that differs from immutable run authority."""
        persisted = {}
        records = self._store.list_repositories(run_id)
        for record in records:
            if (record.resolved_path is None or record.git_common_dir is None
                    or record.repository_identity is None):
                raise cs.RepositoryPathMismatch(
                    "repository identity was never bound for workspace run {0}".format(
                        run_id))
            persisted[record.repository_id] = Path(record.resolved_path)
        if self._repository_paths != persisted:
            raise cs.RepositoryPathMismatch(
                "publication repository paths do not match the persisted binding")
        for record in records:
            self._verify_repository_identity(record)

    def _verify_repository_identity(self, record: cs.RepositoryRecord) -> Path:
        try:
            path = self._repository_paths[record.repository_id]
        except KeyError as exc:
            raise cs.RepositoryPathMismatch(
                "publication path is absent for {0}".format(record.repository_id)) from exc
        try:
            actual = workspace_runtime.repository_binding(path)
        except workspace_runtime.GitEnvironmentalError as exc:
            raise PublicationEnvironmentalError(str(exc)) from exc
        if (actual.resolved_path != record.resolved_path
                or actual.git_common_dir != record.git_common_dir
                or actual.repository_identity != record.repository_identity):
            raise cs.RepositoryPathMismatch(
                "publication repository {0} no longer matches its Git identity".format(
                    record.repository_id))
        return path

    @contextmanager
    def _exclusive_publication_lock(self):
        """Serialize publication across threads and crash-released processes."""
        db_path = str(Path(self._store.db_path).resolve())
        process_lock = _process_publication_lock(db_path)
        with process_lock:
            try:
                with open(db_path + ".publication.lock", "a+") as lock_file:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise PublicationError(
                    "could not acquire publication lock: {0}".format(exc)) from exc

    def _lease_now(self) -> float:
        now = time.time()
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            raise PublicationError("publication lease clock returned a non-numeric value")
        if not math.isfinite(float(now)):
            raise PublicationError("publication lease clock returned a non-finite value")
        return float(now)

    @contextmanager
    def _publication_lease(self, run_id: str):
        """Own the coordinator lease for one bounded publication mutation window."""
        now = self._lease_now()
        if not self._store.acquire_lease(
                run_id, self._actor, now, _PUBLICATION_LEASE_STALE_AFTER_S):
            raise PublicationError(
                "publication refused: another coordinator holds a live lease")
        if self._active_lease_run_id is not None:
            raise PublicationError("publication lease is already active")
        self._active_lease_run_id = run_id
        completed = False
        try:
            yield
            completed = True
        finally:
            self._active_lease_run_id = None
            released = self._store.release_lease(run_id, self._actor)
            if completed and not released:
                raise PublicationError("publication lease was lost before release")

    def _renew_active_publication_lease(self, run_id: str) -> float:
        if self._active_lease_run_id != run_id:
            raise PublicationError("publication mutation has no active coordinator lease")
        now = self._lease_now()
        if not self._store.heartbeat_lease(
                run_id, self._actor, now, _PUBLICATION_LEASE_STALE_AFTER_S):
            raise PublicationError("publication lease was lost before mutation")
        return now

    @staticmethod
    def _validate_target_branch(target_branch: str) -> None:
        try:
            wm.validate_git_branch_ref_fragment(target_branch)
        except ValueError as exc:
            raise PublicationError("target branch is not a valid Git ref fragment") from exc

    def _validate_persisted_target_branches(self, run_id: str) -> None:
        """Reject unsafe persisted refs before opening a lease or Git process."""
        for record in self._store.list_repositories(run_id):
            if record.spec.mode is wm.RepositoryMode.WRITE:
                self._validate_target_branch(record.spec.target_branch or "")
        try:
            intent = self._store.get_publication_intent(run_id)
        except cs.PublicationRefused:
            return
        for target in intent.targets:
            self._validate_target_branch(target.target_branch)

    def _prepare_unlocked(self, run_id: str) -> cs.PublicationIntentRecord:
        """Create an intent only after every writable target is preflighted."""
        self._require_persisted_repository_paths(run_id)
        try:
            return self._store.get_publication_intent(run_id)
        except cs.PublicationRefused:
            pass

        run = self._store.get_run(run_id)
        problems = []
        remote_identities = {}
        auth_repository = None
        for record in self._store.list_repositories(run_id):
            spec = record.spec
            if spec.mode is not wm.RepositoryMode.WRITE:
                continue
            if spec.target_branch is None:
                problems.append("{0}: missing target branch".format(spec.repository_id))
                continue
            try:
                repository = self._repository_path(spec.repository_id)
                if run.workspace.publication_mode is wm.PublicationMode.LOCAL_REFS:
                    actual = self._local_head(repository, spec.target_branch)
                elif run.workspace.publication_mode is wm.PublicationMode.PULL_REQUESTS:
                    if spec.remote is None:
                        raise PublicationError("missing remote")
                    remote_url, remote_repository = self._github_remote(
                        repository, spec.remote)
                    remote_identities[spec.repository_id] = (
                        cs.PullRequestRemoteIdentity(
                            remote_url=remote_url,
                            remote_repository=remote_repository))
                    actual = self._remote_head(
                        repository, remote_url, spec.target_branch, required=True)
                    auth_repository = repository
                else:
                    raise PublicationError("publication mode is none")
            except PublicationEnvironmentalError:
                raise
            except PublicationError as exc:
                problems.append("{0}: {1}".format(spec.repository_id, exc))
                continue
            if actual is None or not _same_sha(actual, spec.base_commit):
                problems.append(
                    "{0}: {1} is {2}, expected {3}".format(
                        spec.repository_id, spec.target_branch,
                        actual or "missing", spec.base_commit))
            if run.workspace.publication_mode is wm.PublicationMode.PULL_REQUESTS:
                if not record.candidate_branch or not record.accepted_sha:
                    problems.append(
                        "{0}: missing accepted candidate branch".format(
                            spec.repository_id))
                    continue
                try:
                    candidate = self._remote_head(
                        repository, remote_url, record.candidate_branch,
                        required=False)
                except PublicationEnvironmentalError:
                    raise
                except PublicationError as exc:
                    problems.append("{0}: {1}".format(spec.repository_id, exc))
                    continue
                if (candidate is not None
                        and not _same_sha(candidate, record.accepted_sha)):
                    problems.append(
                        "{0}: candidate branch {1} is {2}, expected {3}".format(
                            spec.repository_id, record.candidate_branch, candidate,
                            record.accepted_sha))

        if (not problems
                and run.workspace.publication_mode is wm.PublicationMode.PULL_REQUESTS):
            if auth_repository is None:
                problems.append("pull-request publication has no writable repository")
            else:
                try:
                    auth = self._run(("gh", "auth", "status"), auth_repository)
                except PublicationEnvironmentalError:
                    raise
                except PublicationError as exc:
                    problems.append("gh authentication refused: {0}".format(exc))
                else:
                    if auth.returncode != 0:
                        problems.append("gh authentication refused: {0}".format(
                            _command_message(auth)))
        if problems:
            raise PublicationError("publication preflight refused: {0}".format(
                "; ".join(problems)))
        return self._store.prepare_publication(
            run_id, remote_identities=remote_identities, lease_owner=self._actor,
            lease_now=self._renew_active_publication_lease(run_id), actor=self._actor)

    def publish(self, run_id: str) -> PublicationResult:
        """Exclusively publish one durable target vector."""
        self._validate_persisted_target_branches(run_id)
        with self._exclusive_publication_lock():
            with self._publication_lease(run_id):
                return self._publish_unlocked(run_id)

    def _publish_unlocked(self, run_id: str) -> PublicationResult:
        """Apply pending persisted targets in their stored order."""
        intent = self._prepare_unlocked(run_id)
        run = self._store.get_run(run_id)
        mode = run.workspace.publication_mode
        if mode is wm.PublicationMode.LOCAL_REFS:
            return self._publish_local(run_id, intent)
        if mode is wm.PublicationMode.PULL_REQUESTS:
            return self._publish_pull_requests(run_id, intent)
        raise PublicationError("publication mode is none")

    def rollback(self, run_id: str) -> PublicationResult:
        """Exclusively resume a failed local-ref rollback."""
        self._validate_persisted_target_branches(run_id)
        with self._exclusive_publication_lock():
            with self._publication_lease(run_id):
                return self._rollback_unlocked(run_id)

    def _rollback_unlocked(self, run_id: str) -> PublicationResult:
        """Resume a failed local-ref rollback without overwriting moved refs."""
        self._require_persisted_repository_paths(run_id)
        intent = self._store.get_publication_intent(run_id)
        run = self._store.get_run(run_id)
        if run.workspace.publication_mode is not wm.PublicationMode.LOCAL_REFS:
            if intent.state is not wm.PublicationState.FAILED:
                raise PublicationError(
                    "rollback requires a failed pull-request publication intent")
            return self._manual_recovery(
                run_id, "pull-request publication has no automatic rollback")
        if intent.state is wm.PublicationState.PUBLISHED:
            raise PublicationError("cannot roll back a fully published intent")
        if intent.state is wm.PublicationState.PREPARED:
            raise PublicationError("rollback requires a failed publication intent")
        if intent.state is wm.PublicationState.ROLLED_BACK:
            return self._result(run_id)

        targets_to_restore = tuple(
            target for target in reversed(intent.targets)
            if target.state is not wm.PublicationState.ROLLED_BACK)
        for target in targets_to_restore:
            problem = self._rollback_local_target(run_id, target)
            if problem is not None:
                return self._manual_recovery(run_id, problem)
        return self._result(run_id)
    def _rollback_local_target(self, run_id: str, target: cs.PublicationTarget
                               ) -> Optional[str]:
        """Restore one run-owned target only after a state-appropriate proof."""
        try:
            repository = self._repository_path(target.repository_id)
            actual = self._local_head(repository, target.target_branch)
        except PublicationEnvironmentalError:
            raise
        except PublicationError as exc:
            return "{0}: rollback proof refused: {1}".format(
                target.repository_id, exc)
        if _same_sha(actual, target.expected_base_sha):
            self._record_rolled_back(
                run_id, target,
                {"ref": self._local_ref(target.target_branch),
                 "recovered": True,
                 "expected_base_sha": target.expected_base_sha},
            )
            return None
        if target.state is wm.PublicationState.PENDING:
            return "{0}: pending {1} is {2}, expected untouched {3}".format(
                target.repository_id, target.target_branch, actual,
                target.expected_base_sha)
        if not _same_sha(actual, target.accepted_sha):
            return "{0}: {1} is {2}, not accepted {3}".format(
                target.repository_id, target.target_branch, actual,
                target.accepted_sha)
        command = (
            "git", "update-ref", self._local_ref(target.target_branch),
            target.expected_base_sha, target.accepted_sha,
        )
        try:
            result = self._run(command, repository)
        except PublicationEnvironmentalError:
            raise
        except PublicationError as exc:
            return "{0}: rollback command refused: {1}".format(
                target.repository_id, exc)
        if result.returncode == 0:
            self._record_rolled_back(
                run_id, target,
                {"ref": self._local_ref(target.target_branch),
                 "expected_base_sha": target.expected_base_sha,
                 "accepted_sha": target.accepted_sha},
            )
            return None
        try:
            actual_after_failure = self._local_head(repository, target.target_branch)
        except PublicationEnvironmentalError:
            raise
        except PublicationError as exc:
            return "{0}: rollback proof refused: {1}".format(
                target.repository_id, exc)
        if _same_sha(actual_after_failure, target.expected_base_sha):
            self._record_rolled_back(
                run_id, target,
                {"ref": self._local_ref(target.target_branch),
                 "recovered": True,
                 "expected_base_sha": target.expected_base_sha},
            )
            return None
        return "{0}: rollback refused: {1}".format(
            target.repository_id, _command_message(result))

    def _publish_local(self, run_id: str,
                       intent: cs.PublicationIntentRecord) -> PublicationResult:
        if intent.state is wm.PublicationState.FAILED:
            return self._rollback_unlocked(run_id)
        if intent.state is wm.PublicationState.ROLLED_BACK:
            return self._result(run_id)
        if intent.state is wm.PublicationState.PUBLISHED:
            return self._declare_published(run_id)

        for target in intent.targets:
            if target.state is wm.PublicationState.PUBLISHED:
                continue
            if target.state is not wm.PublicationState.PENDING:
                return self._rollback_unlocked(run_id)
            repository = self._repository_path(target.repository_id)
            try:
                actual = self._local_head(repository, target.target_branch)
            except PublicationEnvironmentalError:
                raise
            except PublicationError as exc:
                return self._fail_local(run_id, target, str(exc))
            if _same_sha(actual, target.accepted_sha):
                self._record_published(
                    run_id, target,
                    {"ref": self._local_ref(target.target_branch),
                     "accepted_sha": target.accepted_sha,
                     "recovered": True},
                )
                continue
            if not _same_sha(actual, target.expected_base_sha):
                return self._fail_local(
                    run_id, target,
                    "{0} is {1}, expected {2}".format(
                        target.target_branch, actual, target.expected_base_sha))
            command = (
                "git", "update-ref", self._local_ref(target.target_branch),
                target.accepted_sha, target.expected_base_sha,
            )
            try:
                result = self._run(command, repository)
            except PublicationEnvironmentalError:
                raise
            except PublicationError as exc:
                return self._fail_local(run_id, target, str(exc))
            if result.returncode != 0:
                return self._fail_local(run_id, target, _command_message(result))
            self._record_published(
                run_id, target,
                {"ref": self._local_ref(target.target_branch),
                 "expected_base_sha": target.expected_base_sha,
                 "accepted_sha": target.accepted_sha},
            )

        return self._declare_published(run_id)

    def _publish_pull_requests(self, run_id: str,
                               intent: cs.PublicationIntentRecord) -> PublicationResult:
        if intent.state is wm.PublicationState.PUBLISHED:
            return self._declare_published(run_id)
        if intent.state in (wm.PublicationState.FAILED,
                            wm.PublicationState.ROLLED_BACK):
            return self._manual_recovery(
                run_id, "pull-request publication requires manual recovery")

        records = {record.repository_id: record
                   for record in self._store.list_repositories(run_id)}
        for target in intent.targets:
            if target.state is wm.PublicationState.PUBLISHED:
                continue
            if target.state is wm.PublicationState.PREPARED:
                return self._fail_unexpected_prepared_pull_request_target(
                    run_id, target)
            if target.state is not wm.PublicationState.PENDING:
                return self._manual_recovery(
                    run_id, "unexpected pull-request target state")
            record = records.get(target.repository_id)
            if record is None or record.spec.remote is None:
                return self._fail_pull_request(
                    run_id, target, "missing persisted remote")
            repository = self._repository_path(target.repository_id)
            try:
                remote_url, github_repository = self._verify_pinned_pull_request_remote(
                    repository, record.spec.remote, target)
                actual_target = self._remote_head(
                    repository, remote_url, target.target_branch, required=True)
            except PublicationEnvironmentalError:
                raise
            except PublicationError as exc:
                return self._fail_pull_request(run_id, target, str(exc))
            if actual_target is None or not _same_sha(actual_target,
                                                       target.expected_base_sha):
                return self._fail_pull_request(
                    run_id, target,
                    "{0} is {1}, expected {2}".format(
                        target.target_branch, actual_target or "missing",
                        target.expected_base_sha))

            candidate_ref = self._local_ref(target.candidate_branch)
            try:
                candidate_sha = self._remote_head(
                    repository, remote_url, target.candidate_branch, required=False)
            except PublicationEnvironmentalError:
                raise
            except PublicationError as exc:
                return self._fail_pull_request(run_id, target, str(exc))
            if candidate_sha is None:
                command = (
                    "git", "push", "--force-with-lease={0}:".format(candidate_ref),
                    remote_url, "{0}:{1}".format(target.accepted_sha, candidate_ref),
                )
                try:
                    pushed = self._run(command, repository)
                except PublicationEnvironmentalError:
                    raise
                except PublicationError as exc:
                    return self._fail_pull_request(run_id, target, str(exc))
                if pushed.returncode != 0:
                    return self._fail_pull_request(
                        run_id, target, "candidate push refused: {0}".format(
                            _command_message(pushed)))
            elif not _same_sha(candidate_sha, target.accepted_sha):
                return self._fail_pull_request(
                    run_id, target,
                    "candidate branch {0} already names {1}".format(
                        target.candidate_branch, candidate_sha))

            try:
                existing_url = self._find_open_pull_request(
                    repository, target, github_repository)
            except PublicationEnvironmentalError:
                raise
            except PublicationError as exc:
                return self._fail_pull_request(run_id, target, str(exc))
            if existing_url is not None:
                self._record_published(
                    run_id, target,
                    {"remote": record.spec.remote, "repository": github_repository,
                     "target_branch": target.target_branch,
                     "candidate_branch": target.candidate_branch,
                     "accepted_sha": target.accepted_sha, "url": existing_url,
                     "recovered": True},
                )
                continue

            try:
                created = self._run(
                    ("gh", "pr", "create", "--repo", github_repository,
                     "--base", target.target_branch, "--head",
                     target.candidate_branch, "--fill"), repository)
            except PublicationEnvironmentalError:
                raise
            except PublicationError as exc:
                return self._fail_pull_request(run_id, target, str(exc))
            if created.returncode != 0:
                return self._fail_pull_request(
                    run_id, target, "pull request refused: {0}".format(
                        _command_message(created)))
            url = self._pull_request_url(created.stdout or "", github_repository)
            recovered_after_create = False
            if url is None:
                try:
                    url = self._find_open_pull_request(
                        repository, target, github_repository)
                except PublicationEnvironmentalError:
                    raise
                except PublicationError as exc:
                    return self._fail_pull_request(run_id, target, str(exc))
                if url is None:
                    return self._fail_pull_request(
                        run_id, target,
                        "pull request command returned no unambiguous repository URL")
                recovered_after_create = True
            self._record_published(
                run_id, target,
                {"remote": record.spec.remote, "repository": github_repository,
                 "target_branch": target.target_branch,
                 "candidate_branch": target.candidate_branch,
                 "accepted_sha": target.accepted_sha, "url": url,
                 "recovered": recovered_after_create},
            )

        return self._declare_published(run_id)

    def _fail_local(self, run_id: str, target: cs.PublicationTarget,
                    reason: str) -> PublicationResult:
        self._store.record_publication_step(
            run_id, target.repository_id, wm.PublicationState.FAILED,
            lease_owner=self._actor,
            lease_now=self._renew_active_publication_lease(run_id),
            detail={"reason": reason,
                    "ref": self._local_ref(target.target_branch),
                    "expected_base_sha": target.expected_base_sha,
                    "accepted_sha": target.accepted_sha},
            actor=self._actor,
        )
        result = self._rollback_unlocked(run_id)
        return PublicationResult(
            result.run_id, result.outcome, result.intent, reason, result.steps)

    def _fail_pull_request(self, run_id: str, target: cs.PublicationTarget,
                           reason: str) -> PublicationResult:
        self._store.record_publication_step(
            run_id, target.repository_id, wm.PublicationState.FAILED,
            lease_owner=self._actor,
            lease_now=self._renew_active_publication_lease(run_id),
            detail={"reason": reason,
                    "target_branch": target.target_branch,
                    "candidate_branch": target.candidate_branch,
                    "accepted_sha": target.accepted_sha},
            actor=self._actor,
        )
        return self._manual_recovery(run_id, reason)

    def _record_published(self, run_id: str, target: cs.PublicationTarget,
                          detail: Mapping[str, object]) -> None:
        self._store.record_publication_step(
            run_id, target.repository_id, wm.PublicationState.PUBLISHED,
            lease_owner=self._actor,
            lease_now=self._renew_active_publication_lease(run_id),
            detail=detail, actor=self._actor)

    def _record_rolled_back(self, run_id: str, target: cs.PublicationTarget,
                            detail: Mapping[str, object]) -> None:
        self._store.record_publication_step(
            run_id, target.repository_id, wm.PublicationState.ROLLED_BACK,
            lease_owner=self._actor,
            lease_now=self._renew_active_publication_lease(run_id),
            detail=detail, actor=self._actor)

    def _fail_unexpected_prepared_pull_request_target(
            self, run_id: str, target: cs.PublicationTarget) -> PublicationResult:
        """Persist target-state corruption before entering manual recovery."""
        reason = "unexpected pull-request target state PREPARED"
        self._store.record_unexpected_prepared_target_failure(
            run_id, target.repository_id,
            detail={"reason": reason, "target_state": target.state.value,
                    "target_branch": target.target_branch,
                    "candidate_branch": target.candidate_branch,
                    "accepted_sha": target.accepted_sha},
            lease_owner=self._actor,
            lease_now=self._renew_active_publication_lease(run_id),
            actor=self._actor,
        )
        return self._manual_recovery(run_id, reason)

    def _declare_published(self, run_id: str) -> PublicationResult:
        run = self._store.get_run(run_id)
        if run.outcome is wm.WorkspaceOutcome.ACCEPTED:
            self._renew_active_publication_lease(run_id)
            self._store.declare_outcome(
                run_id, wm.WorkspaceOutcome.PUBLISHED, actor=self._actor,
                lease_owner=self._actor)
        return self._result(run_id)

    def _manual_recovery(self, run_id: str, reason: str) -> PublicationResult:
        run = self._store.get_run(run_id)
        if run.outcome is wm.WorkspaceOutcome.ACCEPTED:
            self._renew_active_publication_lease(run_id)
            self._store.declare_outcome(
                run_id, wm.WorkspaceOutcome.PARTIALLY_PUBLISHED,
                actor=self._actor, lease_owner=self._actor)
            run = self._store.get_run(run_id)
        if run.outcome is wm.WorkspaceOutcome.PARTIALLY_PUBLISHED:
            self._renew_active_publication_lease(run_id)
            self._store.declare_outcome(
                run_id, wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED,
                actor=self._actor, lease_owner=self._actor)
        return self._result(run_id, reason)

    def _result(self, run_id: str, reason: Optional[str] = None) -> PublicationResult:
        run = self._store.get_run(run_id)
        if run.outcome is None:
            raise PublicationError("publication run has no workspace outcome")
        return PublicationResult(
            run_id=run_id, outcome=run.outcome,
            intent=self._store.get_publication_intent(run_id), reason=reason,
            steps=self._store.list_publication_steps(run_id))

    def _repository_path(self, repository_id: str) -> Path:
        record = self._store.get_repository(self._active_lease_run_id or "", repository_id)
        return self._verify_repository_identity(record)

    def _run(self, argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
        if self._active_lease_run_id is not None:
            self._renew_active_publication_lease(self._active_lease_run_id)
        try:
            result = self._command_runner(tuple(argv), cwd)
        except OSError as exc:
            raise PublicationEnvironmentalError(
                "could not run {0}: {1}".format(argv[0], exc)) from exc
        if not isinstance(result, subprocess.CompletedProcess):
            raise PublicationEnvironmentalError(
                "command runner returned no CompletedProcess")
        return result

    def _local_head(self, repository: Path, branch: str) -> str:
        result = self._run(
            ("git", "rev-parse", "--verify", "--quiet",
             "{0}^{{commit}}".format(self._local_ref(branch))), repository)
        value = (result.stdout or "").strip()
        if result.returncode == 1 and not value:
            raise PublicationError(
                "local ref {0} does not resolve".format(self._local_ref(branch)))
        if result.returncode != 0:
            raise PublicationEnvironmentalError(
                "local ref {0} could not be read: {1}".format(
                    self._local_ref(branch), _command_message(result)))
        if not value:
            raise PublicationEnvironmentalError(
                "local ref {0} returned no commit".format(self._local_ref(branch)))
        return value

    def _github_remote(self, repository: Path, remote: str) -> Tuple[str, str]:
        """Bind a declared Git remote to one supported GitHub repository."""
        if remote.startswith("-"):
            raise PublicationError("remote names must not begin with '-'")
        result = self._run(("git", "remote", "get-url", "--", remote), repository)
        if result.returncode != 0:
            raise PublicationEnvironmentalError(
                "remote {0} URL could not be read: {1}".format(
                    remote, _command_message(result)))
        remote_url = (result.stdout or "").strip()
        if not remote_url or "\n" in remote_url:
            raise PublicationError(
                "remote {0} has no unambiguous URL".format(remote))
        return remote_url, self._github_repository_from_url(remote_url)

    def _verify_pinned_pull_request_remote(
            self, repository: Path, remote: str,
            target: cs.PublicationTarget) -> Tuple[str, str]:
        """Prove the mutable remote name still names the prepared authority."""
        if target.remote_url is None or target.remote_repository is None:
            raise PublicationError(
                "prepared pull-request target lacks its remote identity")
        current_url, current_repository = self._github_remote(repository, remote)
        if (current_url != target.remote_url
                or current_repository != target.remote_repository):
            raise PublicationError(
                "remote {0} no longer matches prepared repository authority".format(
                    remote))
        return target.remote_url, target.remote_repository

    def _remote_head(self, repository: Path, remote: str, branch: str, *,
                     required: bool) -> Optional[str]:
        ref = self._local_ref(branch)
        result = self._run(("git", "ls-remote", "--", remote, ref), repository)
        if result.returncode != 0:
            raise PublicationEnvironmentalError(
                "remote ref {0} could not be read: {1}".format(
                    ref, _command_message(result)))
        values = []
        for line in (result.stdout or "").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == ref:
                values.append(fields[0])
        if len(values) != 1:
            if not values and not required:
                return None
            if not values:
                raise PublicationError("remote ref {0} does not resolve".format(ref))
            raise PublicationError("remote ref {0} is ambiguous".format(ref))
        return values[0]

    def _find_open_pull_request(self, repository: Path,
                                target: cs.PublicationTarget,
                                github_repository: str) -> Optional[str]:
        """Return the sole accepted-SHA PR in the explicitly bound repository."""
        listed = self._run(
            ("gh", "pr", "list", "--repo", github_repository,
             "--head", target.candidate_branch, "--base", target.target_branch,
             "--state", "open", "--json", "url,headRefOid", "--limit", "2"),
            repository)
        if listed.returncode != 0:
            raise PublicationError(
                "open pull request lookup refused: {0}".format(
                    _command_message(listed)))
        return self._open_pull_request_url(
            listed.stdout or "", target.accepted_sha, github_repository)

    @staticmethod
    def _github_repository_from_url(remote_url: str) -> str:
        """Parse one supported GitHub remote URL into ``owner/repository``."""
        scp = re.fullmatch(
            r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?", remote_url)
        if scp is not None:
            owner, repository = scp.groups()
        else:
            try:
                parsed = urlparse(remote_url)
                port = parsed.port
            except ValueError as exc:
                raise PublicationError("remote URL has an invalid port") from exc
            if parsed.scheme == "https":
                supported = (
                    parsed.hostname is not None
                    and parsed.hostname.lower() == "github.com"
                    and parsed.username is None
                    and parsed.password is None
                    and port in (None, 443)
                )
            elif parsed.scheme == "ssh":
                supported = (
                    parsed.hostname is not None
                    and parsed.hostname.lower() == "github.com"
                    and parsed.username == "git"
                    and parsed.password is None
                    and port in (None, 22)
                )
            else:
                supported = False
            if (not supported or parsed.params or parsed.query or parsed.fragment):
                raise PublicationError(
                    "remote URL is not a supported GitHub HTTPS or SSH URL")
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 2:
                raise PublicationError(
                    "remote URL must name exactly one GitHub owner/repository")
            owner, repository = parts
            if repository.endswith(".git"):
                repository = repository[:-4]

        component = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
        if not component.fullmatch(owner) or not component.fullmatch(repository):
            raise PublicationError(
                "remote URL must name a valid GitHub owner/repository")
        return "{0}/{1}".format(owner, repository)

    @staticmethod
    def _github_pull_request_url(url: str,
                                 github_repository: str) -> Optional[str]:
        """Return ``url`` only when it is a PR URL in ``github_repository``."""
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return None
        if (parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.hostname.lower() != "github.com"
                or parsed.username is not None
                or parsed.password is not None
                or port not in (None, 443)
                or parsed.params
                or parsed.query
                or parsed.fragment):
            return None
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
            return None
        expected_owner, expected_repository = github_repository.split("/", 1)
        if (parts[0].lower() != expected_owner.lower()
                or parts[1].lower() != expected_repository.lower()):
            return None
        return url

    @classmethod
    def _open_pull_request_url(cls, output: str, accepted_sha: str,
                               github_repository: str) -> Optional[str]:
        """Return the sole open matching PR, refusing ambiguous recovery data."""
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise PublicationError("open pull request lookup returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise PublicationError("open pull request lookup returned a non-list")
        if not payload:
            return None
        if len(payload) != 1 or not isinstance(payload[0], dict):
            raise PublicationError("open pull request lookup is not unique")
        pull_request = payload[0]
        url = pull_request.get("url")
        head_sha = pull_request.get("headRefOid")
        if not isinstance(url, str):
            raise PublicationError("open pull request has no URL")
        if cls._github_pull_request_url(url, github_repository) is None:
            raise PublicationError(
                "open pull request URL does not match the declared repository")
        if not isinstance(head_sha, str) or not _same_sha(head_sha, accepted_sha):
            raise PublicationError("open pull request head does not match accepted SHA")
        return url

    @staticmethod
    def _local_ref(branch: str) -> str:
        WorkspacePublisher._validate_target_branch(branch)
        return "refs/heads/{0}".format(branch)

    @classmethod
    def _pull_request_url(cls, output: str,
                          github_repository: str) -> Optional[str]:
        urls = []
        for token in output.split():
            url = cls._github_pull_request_url(token, github_repository)
            if url is not None and url not in urls:
                urls.append(url)
        return urls[0] if len(urls) == 1 else None
