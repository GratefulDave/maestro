"""Strict subprocess protocol for one workspace participant repository."""
from __future__ import annotations
import json
import math
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Protocol, Tuple
from .launcher import quiesce_process_group

PARTICIPANT_RESULT_SCHEMA = "maestro-participant-result.v1"
_GIT_OBJECT = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_OUTCOMES = frozenset(("accepted", "blocked", "cancelled"))
_RESULT_KEYS = frozenset(("schema", "child_run_id", "outcome", "accepted_sha", "reason"))
_MAX_RESULT_BYTES = 64 * 1024
ParticipantIdentity = Tuple[str, str]

class ParticipantProtocolError(RuntimeError):
    """The child did not honour the closed participant-result protocol."""

class ParticipantExecutionError(RuntimeError):
    """The child process did not complete its declared command successfully."""
    def __init__(self, message: str, *, stdout_tail: Tuple[str, ...] = (), stderr_tail: Tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.stdout_tail = tuple(stdout_tail)
        self.stderr_tail = tuple(stderr_tail)
        self.diagnostic_tail = self.stdout_tail + self.stderr_tail

class ParticipantCancelled(ParticipantExecutionError):
    """The coordinator cancelled an owned participant process group."""

@dataclass(frozen=True)
class ParticipantContext:
    workspace_run_id: str
    repository_id: str
    child_run_id: str
    plan_path: Path
    plan_digest: str
    candidate_branch: str
    candidate_worktree: Path
    participant_result_path: Path
    run_argv: Tuple[str, ...]
    def __post_init__(self) -> None:
        _require_nonempty_string("workspace_run_id", self.workspace_run_id)
        _require_nonempty_string("repository_id", self.repository_id)
        _require_nonempty_string("child_run_id", self.child_run_id)
        _require_path("plan_path", self.plan_path)
        if type(self.plan_digest) is not str or not _SHA256.fullmatch(self.plan_digest):
            raise ValueError("plan_digest must be a 64-character hexadecimal digest")
        _require_nonempty_string("candidate_branch", self.candidate_branch)
        _require_path("candidate_worktree", self.candidate_worktree)
        _require_path("participant_result_path", self.participant_result_path)
        candidate_worktree = self.candidate_worktree.resolve()
        plan_path = self.plan_path.resolve()
        participant_result_path = self.participant_result_path.resolve()
        if not _is_inside(plan_path, candidate_worktree):
            raise ValueError("plan_path must be inside candidate_worktree")
        if _is_inside(participant_result_path, candidate_worktree):
            raise ValueError("participant_result_path must be outside candidate_worktree")
        object.__setattr__(self, "candidate_worktree", candidate_worktree)
        object.__setattr__(self, "plan_path", plan_path)
        object.__setattr__(self, "participant_result_path", participant_result_path)
        if type(self.run_argv) is not tuple or not self.run_argv:
            raise ValueError("run_argv must be a non-empty tuple of strings")
        for argument in self.run_argv:
            _require_nonempty_string("run_argv item", argument)

@dataclass(frozen=True)
class ParticipantResult:
    schema: str
    child_run_id: str
    outcome: str
    accepted_sha: Optional[str]
    reason: str
    def __post_init__(self) -> None:
        if self.schema != PARTICIPANT_RESULT_SCHEMA:
            raise ValueError("participant result schema is invalid")
        _require_nonempty_string("child_run_id", self.child_run_id)
        if type(self.outcome) is not str or self.outcome not in _OUTCOMES:
            raise ValueError("participant result outcome is invalid")
        if self.accepted_sha is not None and (type(self.accepted_sha) is not str or not _GIT_OBJECT.fullmatch(self.accepted_sha)):
            raise ValueError("participant result accepted_sha is invalid")
        if self.outcome == "accepted" and self.accepted_sha is None:
            raise ValueError("accepted participant result requires accepted_sha")
        if self.outcome != "accepted" and self.accepted_sha is not None:
            raise ValueError("non-accepted participant result forbids accepted_sha")
        if type(self.reason) is not str:
            raise ValueError("participant result reason must be a string")

class ParticipantRunner(Protocol):
    def run(self, context: ParticipantContext, *, timeout: float) -> ParticipantResult: ...
    def cancel(self, workspace_run_id: str, repository_id: str, deadline: float) -> bool: ...

@dataclass
class _ActiveProcess:
    process: subprocess.Popen
    process_group: int
    cancelled: bool = False
    quiesced: bool = False
    worker_exited: bool = False
    cancel_finished: threading.Event = field(default_factory=threading.Event)

class SubprocessParticipantRunner:
    def __init__(self, *, diagnostic_tail_lines: int = 20) -> None:
        if type(diagnostic_tail_lines) is not int or diagnostic_tail_lines < 1:
            raise ValueError("diagnostic_tail_lines must be a positive integer")
        self._diagnostic_tail_lines = diagnostic_tail_lines
        self._lock = threading.RLock()
        self._processes: Dict[ParticipantIdentity, _ActiveProcess] = {}
        self._cancelled_identities = set()

    @property
    def active_participant_ids(self) -> Tuple[ParticipantIdentity, ...]:
        with self._lock:
            return tuple(sorted(self._processes))

    def run(self, context: ParticipantContext, *, timeout: float) -> ParticipantResult:
        if not isinstance(context, ParticipantContext):
            raise TypeError("context must be ParticipantContext")
        _require_timeout(timeout)
        identity = (context.workspace_run_id, context.repository_id)
        result_path = context.participant_result_path
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ParticipantProtocolError("PARTICIPANT_RESULT_DIRECTORY_FAILED: {0}".format(exc)) from exc
        environment = _participant_environment(context)
        launch_started_ns = time.time_ns()
        with self._lock:
            if identity in self._processes:
                raise ParticipantExecutionError("PARTICIPANT_ALREADY_RUNNING workspace_run_id={0} repository_id={1}".format(*identity))
            if identity in self._cancelled_identities:
                raise ParticipantCancelled(
                    "PARTICIPANT_CANCELLED repository_id={0}".format(
                        context.repository_id))
            if os.path.lexists(str(result_path)):
                raise ParticipantProtocolError("PARTICIPANT_RESULT_STALE repository_id={0}".format(context.repository_id))
            try:
                process = subprocess.Popen(list(context.run_argv), cwd=str(context.candidate_worktree), env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            except (OSError, ValueError) as exc:
                raise ParticipantExecutionError("PARTICIPANT_LAUNCH_FAILED repository_id={0}: {1}".format(context.repository_id, exc)) from exc
            # start_new_session makes the leader PID the owned group identity;
            # retain it before communicate() can reap that leader.
            active = _ActiveProcess(process, process.pid)
            self._processes[identity] = active
        completed = False
        cleanup_proven = False
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # communicate() also waits for EOF on stdout/stderr.  A leader
                # that has exited can therefore time out solely because an
                # owned descendant inherited one of those pipe descriptors.
                # Capture terminal status before signaling the group: only an
                # already-terminal leader can be adjudicated after cleanup.
                leader_returncode = process.poll()
                cleanup_proven = _quiesce_owned_group(
                    active.process_group, time.monotonic() + 1.0)
                if cleanup_proven:
                    with self._lock:
                        active.quiesced = True
                stdout, stderr = _collect_after_quiesce(process)
                if not cleanup_proven:
                    cleanup_proven = _group_absent(active.process_group)
                    if cleanup_proven:
                        with self._lock:
                            active.quiesced = True
                if not cleanup_proven or leader_returncode is None:
                    raise ParticipantExecutionError(
                        "PARTICIPANT_TIMEOUT repository_id={0}".format(
                            context.repository_id),
                        stdout_tail=_tail(stdout, self._diagnostic_tail_lines),
                        stderr_tail=_tail(stderr, self._diagnostic_tail_lines))
            stdout_tail = _tail(stdout, self._diagnostic_tail_lines)
            stderr_tail = _tail(stderr, self._diagnostic_tail_lines)
            if active.cancelled:
                active.cancel_finished.wait(timeout=1.1)
                raise ParticipantCancelled("PARTICIPANT_CANCELLED repository_id={0}".format(context.repository_id), stdout_tail=stdout_tail, stderr_tail=stderr_tail)
            if process.returncode != 0:
                raise ParticipantExecutionError("PARTICIPANT_EXIT_FAILED repository_id={0} exit_code={1}".format(context.repository_id, process.returncode), stdout_tail=stdout_tail, stderr_tail=stderr_tail)
            if not cleanup_proven:
                cleanup_proven = _quiesce_owned_group(
                    active.process_group, time.monotonic() + 1.0)
                if not cleanup_proven:
                    raise ParticipantExecutionError(
                        "PARTICIPANT_CLEANUP_UNPROVEN repository_id={0}".format(
                            context.repository_id),
                        stdout_tail=stdout_tail, stderr_tail=stderr_tail)
                with self._lock:
                    active.quiesced = True
            result = _load_participant_result(result_path, context.child_run_id, launch_started_ns)
            completed = True
            return result
        except BaseException:
            with self._lock:
                shared_cleanup_proven = active.quiesced
            if not cleanup_proven and not shared_cleanup_proven:
                cleanup_proven = _quiesce_owned_group(
                    active.process_group, time.monotonic() + 1.0)
                if cleanup_proven:
                    with self._lock:
                        active.quiesced = True
            raise
        finally:
            with self._lock:
                active.worker_exited = True
                if (completed or cleanup_proven or active.quiesced) and (
                        self._processes.get(identity) is active):
                    del self._processes[identity]

    def cancel(self, workspace_run_id: str, repository_id: str, deadline: float) -> bool:
        active = None
        try:
            identity = (workspace_run_id, repository_id)
            with self._lock:
                self._cancelled_identities.add(identity)
                active = self._processes.get(identity)
                if active is None:
                    return True
                active.cancelled = True
                process_group = active.process_group
            proven = _quiesce_owned_group(process_group, deadline)
            if not proven:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining:
                    try:
                        active.process.wait(timeout=remaining)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                proven = _group_absent(process_group)
            with self._lock:
                if proven:
                    active.quiesced = True
                    if active.worker_exited and self._processes.get(identity) is active:
                        del self._processes[identity]
            return proven
        except BaseException:
            return False
        finally:
            if active is not None:
                active.cancel_finished.set()

def _participant_environment(context: ParticipantContext) -> Mapping[str, str]:
    environment = dict(os.environ)
    environment.update({"MAESTRO_WORKSPACE_RUN_ID": context.workspace_run_id, "MAESTRO_REPOSITORY_ID": context.repository_id, "MAESTRO_CHILD_RUN_ID": context.child_run_id, "MAESTRO_PLAN_PATH": str(context.plan_path), "MAESTRO_PLAN_DIGEST": context.plan_digest, "MAESTRO_CANDIDATE_BRANCH": context.candidate_branch, "MAESTRO_CANDIDATE_WORKTREE": str(context.candidate_worktree), "MAESTRO_PARTICIPANT_RESULT_PATH": str(context.participant_result_path)})
    return environment

def load_participant_result(path: Path, expected_child_run_id: str) -> ParticipantResult:
    return _load_participant_result(path, expected_child_run_id, None)

def _load_participant_result(path: Path, expected_child_run_id: str, minimum_mtime_ns: Optional[int]) -> ParticipantResult:
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    _require_nonempty_string("expected_child_run_id", expected_child_run_id)
    raw, result_stat = _read_result_file(path)
    if minimum_mtime_ns is not None and result_stat.st_mtime_ns < minimum_mtime_ns:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_STALE")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_MALFORMED: {0}".format(exc)) from exc
    if type(payload) is not dict:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_MUST_BE_OBJECT")
    if frozenset(payload) != _RESULT_KEYS:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_KEYS_INVALID")
    try:
        result = ParticipantResult(schema=payload["schema"], child_run_id=payload["child_run_id"], outcome=payload["outcome"], accepted_sha=payload["accepted_sha"], reason=payload["reason"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_FIELDS_INVALID: {0}".format(exc)) from exc
    if result.child_run_id != expected_child_run_id:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_CHILD_RUN_ID_MISMATCH")
    return result

def _read_result_file(path: Path) -> Tuple[bytes, os.stat_result]:
    try:
        descriptor = _open_result_file(path)
    except FileNotFoundError as exc:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_ABSENT") from exc
    except OSError as exc:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_UNSAFE_PATH: {0}".format(exc)) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ParticipantProtocolError("PARTICIPANT_RESULT_NOT_REGULAR")
        if before.st_size > _MAX_RESULT_BYTES:
            raise ParticipantProtocolError("PARTICIPANT_RESULT_TOO_LARGE")
        chunks = []
        total = 0
        while total <= _MAX_RESULT_BYTES:
            chunk = os.read(descriptor, min(8192, _MAX_RESULT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_RESULT_BYTES:
            raise ParticipantProtocolError("PARTICIPANT_RESULT_TOO_LARGE")
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ParticipantProtocolError("PARTICIPANT_RESULT_UNREADABLE: {0}".format(exc)) from exc
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ParticipantProtocolError("PARTICIPANT_RESULT_REPOINTED")
    return b"".join(chunks), after

def _open_result_file(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("secure no-follow result loading is unavailable")
    absolute = path.absolute()
    if not absolute.name:
        raise OSError("participant result path has no file name")
    parent_descriptor = os.open(absolute.anchor, os.O_RDONLY | directory | nofollow)
    try:
        for component in absolute.parts[1:-1]:
            child_descriptor = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = child_descriptor
        return os.open(absolute.name, os.O_RDONLY | nofollow, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)

def _unique_object(pairs: object) -> dict:
    result = {}
    for key, value in pairs:  # type: ignore[union-attr]
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result

def _tail(value: Optional[str], limit: int) -> Tuple[str, ...]:
    return () if not value else tuple(value.splitlines()[-limit:])

def _collect_after_quiesce(process: subprocess.Popen) -> Tuple[str, str]:
    """Bound pipe collection after the owned group has been proven absent."""
    try:
        return process.communicate(timeout=1.1)
    except subprocess.TimeoutExpired:
        # Do not signal the numeric process-group identity again: absence has
        # already been proved, so a reused PGID cannot be ours.
        return "", ""

def _quiesce_owned_group(process_group: int, deadline: float) -> bool:
    try:
        quiesce_process_group(process_group, deadline)
    except BaseException:
        return False
    return _group_absent(process_group)

def _group_absent(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False

def _is_inside(candidate: Path, boundary: Path) -> bool:
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True

def _require_nonempty_string(name: str, value: object) -> None:
    if type(value) is not str or not value:
        raise ValueError("{0} must be a non-empty string".format(name))

def _require_path(name: str, value: object) -> None:
    if not isinstance(value, Path):
        raise ValueError("{0} must be a pathlib.Path".format(name))

def _require_timeout(timeout: object) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    numeric_timeout = float(timeout)
    if not math.isfinite(numeric_timeout) or numeric_timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
