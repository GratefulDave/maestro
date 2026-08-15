"""Authoritative, durable state for a Maestro multi-repository workspace run.

The coordinator is deliberately a small SQLite authority.  It owns the
workspace projection, repository lifecycle, integration-gate results,
publication recovery data, leases, and an append-only transition log.  It does
not calculate a workspace digest or canonicalize workspace input: callers hand
it the digest of the bytes they stored, and that exact value is retained.
"""

from __future__ import annotations

import functools
import math
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from . import workspace_model as wm
from .utils import now_iso


class CoordinatorStoreError(RuntimeError):
    """Base class for coordinator-store refusals."""

class CoordinatorDatabaseUnavailable(CoordinatorStoreError):
    """The requested coordinator database is not an existing regular file."""


class UnknownRun(CoordinatorStoreError):
    """No workspace run exists for the supplied identifier."""


class UnknownRepository(CoordinatorStoreError):
    """The workspace run does not contain the supplied repository."""


class RunAlreadyExists(CoordinatorStoreError):
    """A run identifier cannot be projected twice."""


class IllegalTransition(CoordinatorStoreError):
    """A repository or workspace outcome transition is not legal."""


class GateAlreadyRecorded(CoordinatorStoreError):
    """An integration gate has an immutable authoritative result already."""


class DuplicatePublicationIntent(CoordinatorStoreError):
    """A publication target vector is create-once recovery evidence."""


class PublicationRefused(CoordinatorStoreError):
    """A publication operation is outside its legal workspace state."""


class LeaseOwnershipError(CoordinatorStoreError):
    """A coordinator mutation was attempted without its live lease token."""


class RepositoryPathMismatch(CoordinatorStoreError):
    """Repository paths differ from the concrete paths bound for this run."""

@dataclass(frozen=True)
class WorkspaceRunRecord:
    """The durable workspace-run authority row and its frozen workspace plan."""

    run_id: str
    workspace_id: str
    workspace_digest: str
    workspace: wm.WorkspacePlan
    outcome: Optional[wm.WorkspaceOutcome]
    cancel_requested: bool
    created_at: str
    lease_owner: Optional[str]
    lease_expires_at: Optional[float]


@dataclass(frozen=True)
class RepositoryRecord:
    """A persisted repository spec together with its current lifecycle."""

    run_id: str
    repository_id: str
    position: int
    spec: wm.RepositorySpec
    state: wm.RepositoryState
    child_run_id: Optional[str]
    candidate_branch: Optional[str]
    accepted_sha: Optional[str]
    resolved_path: Optional[str]
    git_common_dir: Optional[str]
    repository_identity: Optional[str]
    block_reason: Optional[str]
    updated_at: str


@dataclass(frozen=True)
class RepositoryPathBinding:
    """One canonical Git worktree root and its immutable common-dir identity."""

    resolved_path: str
    git_common_dir: str
    repository_identity: str


@dataclass(frozen=True)
class GateRecord:
    """One immutable result for a workspace integration gate."""

    run_id: str
    gate_index: int
    passed: bool
    detail: Mapping[str, Any]
    recorded_at: str


@dataclass(frozen=True)
class PublicationTarget:
    """One durable member of the ordered publication target vector."""

    repository_id: str
    expected_base_sha: str
    target_branch: str
    candidate_branch: str
    accepted_sha: str
    remote_url: Optional[str]
    remote_repository: Optional[str]
    state: wm.PublicationState


@dataclass(frozen=True)
class PullRequestRemoteIdentity:
    """The resolved remote URL and GitHub repository authorized at prepare."""

    remote_url: str
    remote_repository: str

@dataclass(frozen=True)
class PublicationIntentRecord:
    """The create-once publication intent and its ordered recovery vector."""

    run_id: str
    state: wm.PublicationState
    targets: Tuple[PublicationTarget, ...]
    prepared_at: str
    updated_at: str


@dataclass(frozen=True)
class PublicationStepRecord:
    """An append-only change to one publication target's state."""

    step_id: int
    run_id: str
    repository_id: str
    from_state: wm.PublicationState
    to_state: wm.PublicationState
    detail: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True)
class TransitionRecord:
    """An audit-only transition; it is never used as runtime authority."""

    transition_id: int
    run_id: str
    repository_id: Optional[str]
    kind: str
    from_state: Optional[Any]
    to_state: Optional[Any]
    reason: str
    actor: str
    detail: Mapping[str, Any]
    created_at: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_runs (
  run_id            TEXT PRIMARY KEY,
  workspace_id      TEXT NOT NULL,
  workspace_digest  TEXT NOT NULL,
  workspace_json    TEXT NOT NULL,
  outcome           TEXT,
  cancel_requested  INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  lease_owner       TEXT,
  lease_expires_at  REAL
);
CREATE TABLE IF NOT EXISTS workspace_repositories (
  run_id            TEXT NOT NULL REFERENCES workspace_runs(run_id),
  repository_id     TEXT NOT NULL,
  position          INTEGER NOT NULL,
  spec_json         TEXT NOT NULL,
  needs_json        TEXT NOT NULL,
  state             TEXT NOT NULL,
  child_run_id      TEXT,
  resolved_path     TEXT,
  git_common_dir    TEXT,
  repository_identity TEXT,
  identity_binding_state INTEGER NOT NULL DEFAULT 0,
  candidate_branch  TEXT,
  accepted_sha      TEXT,
  block_reason      TEXT,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (run_id, repository_id),
  UNIQUE (run_id, position)
);
CREATE TABLE IF NOT EXISTS workspace_gates (
  run_id       TEXT NOT NULL REFERENCES workspace_runs(run_id),
  gate_index   INTEGER NOT NULL,
  passed       INTEGER NOT NULL,
  detail_json  TEXT NOT NULL DEFAULT '{}',
  recorded_at  TEXT NOT NULL,
  PRIMARY KEY (run_id, gate_index)
);
CREATE TABLE IF NOT EXISTS publication_intents (
  run_id       TEXT PRIMARY KEY REFERENCES workspace_runs(run_id),
  state        TEXT NOT NULL,
  prepared_at  TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_targets (
  run_id            TEXT NOT NULL REFERENCES publication_intents(run_id),
  position          INTEGER NOT NULL,
  repository_id     TEXT NOT NULL,
  expected_base_sha TEXT NOT NULL,
  target_branch     TEXT NOT NULL,
  candidate_branch  TEXT NOT NULL,
  accepted_sha      TEXT NOT NULL,
  remote_url        TEXT,
  remote_repository TEXT,
  state             TEXT NOT NULL,
  PRIMARY KEY (run_id, repository_id),
  UNIQUE (run_id, position)
);
CREATE TABLE IF NOT EXISTS publication_steps (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL REFERENCES publication_intents(run_id),
  repository_id   TEXT NOT NULL,
  from_state      TEXT NOT NULL,
  to_state        TEXT NOT NULL,
  detail_json     TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coordinator_transitions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL REFERENCES workspace_runs(run_id),
  repository_id   TEXT,
  kind            TEXT NOT NULL,
  from_state      TEXT,
  to_state        TEXT,
  reason          TEXT NOT NULL,
  actor           TEXT NOT NULL,
  detail_json     TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_repositories_state
  ON workspace_repositories(run_id, state);
CREATE INDEX IF NOT EXISTS idx_coordinator_transitions_run
  ON coordinator_transitions(run_id, id);
CREATE INDEX IF NOT EXISTS idx_publication_steps_run
  ON publication_steps(run_id, id);
"""


_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def _enable_wal(conn: sqlite3.Connection, attempts: int = 50) -> None:
    """Enable WAL while tolerating another process opening the same database."""
    for _ in range(attempts):
        current = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if str(current).lower() == "wal":
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            return
        except sqlite3.OperationalError as error:
            text = str(error).lower()
            if "locked" not in text and "busy" not in text:
                raise
            time.sleep(0.01)
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    if str(mode).lower() != "wal":
        raise sqlite3.OperationalError(
            "could not enable WAL for coordinator store; journal mode is {0!r}".format(mode))


def serialized(method):
    """Serialize every public operation over the store's shared connection."""
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


def transactional_mutation(method):
    """Run one public state change under the store lock and write transaction."""
    @functools.wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                result = method(self, *args, **kwargs)
                self.conn.execute("COMMIT")
                return result
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
    return guarded


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _decode_mapping(value: str) -> Mapping[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise CoordinatorStoreError("stored detail must be a JSON object")
    return _frozen_mapping(decoded)


def _enum_or_none(enum_type, value):
    return enum_type(value) if value is not None else None


def _is_sha(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(_SHA_PATTERN.fullmatch(value))


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add nullable identity authority to databases created before path binding."""
    repository_columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(workspace_repositories)").fetchall()
    }
    for column in ("resolved_path", "git_common_dir", "repository_identity"):
        if column not in repository_columns:
            conn.execute(
                "ALTER TABLE workspace_repositories ADD COLUMN {0} TEXT".format(
                    column))
    if "identity_binding_state" not in repository_columns:
        # Pre-identity rows cannot be safely resumed or rebound as a new clone.
        conn.execute(
            "ALTER TABLE workspace_repositories ADD COLUMN"
            " identity_binding_state INTEGER NOT NULL DEFAULT -1")
    publication_columns = {
        row["name"] for row in conn.execute(
            "PRAGMA table_info(publication_targets)").fetchall()
    }
    for column in ("remote_url", "remote_repository"):
        if column not in publication_columns:
            conn.execute(
                "ALTER TABLE publication_targets ADD COLUMN {0} TEXT".format(
                    column))

class CoordinatorStore:
    """Thread-safe SQLite authority for one or more workspace runs.

    Public writes use ``BEGIN IMMEDIATE`` so their guard, authority update, and
    audit append commit together.  The one shared connection is protected by a
    re-entrant lock, matching the lifecycle store's threading contract.
    """

    def __init__(self, db_path, *, create: bool = True):
        path = Path(db_path)
        self.db_path = str(path)
        self._lock = threading.RLock()
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.db_path, isolation_level=None,
                                        check_same_thread=False)
        else:
            if not path.is_file():
                raise CoordinatorDatabaseUnavailable(
                    "coordinator database is not an existing regular file: {0}".format(
                        path))
            try:
                self.conn = sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro", uri=True,
                    isolation_level=None, check_same_thread=False)
            except sqlite3.OperationalError:
                if not path.is_file():
                    raise CoordinatorDatabaseUnavailable(
                        "coordinator database is not an existing regular file: {0}".format(
                            path))
                raise
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000;")
        if create:
            self.conn.execute("PRAGMA foreign_keys=ON;")
            _enable_wal(self.conn)
            self.conn.execute("PRAGMA synchronous=FULL;")
            self.conn.executescript(SCHEMA)
            _migrate_schema(self.conn)
        repository_columns = {
            row["name"] for row in self.conn.execute(
                "PRAGMA table_info(workspace_repositories)").fetchall()
        }
        self._has_repository_identity_columns = {
            "resolved_path", "git_common_dir", "repository_identity",
            "identity_binding_state",
        }.issubset(repository_columns)

    @classmethod
    def open_existing(cls, db_path):
        """Open an existing coordinator database without changing it."""
        return cls(db_path, create=False)

    # ── run / repository projection ──────────────────────────────────────

    @transactional_mutation
    def create_run(self, run_id: str, workspace_digest: str,
                   workspace: wm.WorkspacePlan) -> WorkspaceRunRecord:
        """Create one run and atomically project all exact repository specs."""
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a nonempty string")
        if not isinstance(workspace_digest, str) or not workspace_digest:
            raise ValueError("workspace_digest must be a nonempty string")
        existing = self.conn.execute(
            "SELECT 1 FROM workspace_runs WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None:
            raise RunAlreadyExists("workspace run {0} already exists".format(run_id))

        now = now_iso()
        workspace_json = json.dumps(workspace.model_dump(mode="json"),
                                    sort_keys=True, separators=(",", ":"))
        try:
            self.conn.execute(
                "INSERT INTO workspace_runs (run_id, workspace_id, workspace_digest,"
                " workspace_json, outcome, cancel_requested, created_at, lease_owner,"
                " lease_expires_at) VALUES (?,?,?,?,NULL,0,?,NULL,NULL)",
                (run_id, workspace.workspace_id, workspace_digest, workspace_json, now))
            for position, spec in enumerate(workspace.repositories):
                spec_json = json.dumps(spec.model_dump(mode="json"),
                                       sort_keys=True, separators=(",", ":"))
                self.conn.execute(
                    "INSERT INTO workspace_repositories (run_id, repository_id, position,"
                    " spec_json, needs_json, state, child_run_id, candidate_branch,"
                    " accepted_sha, block_reason, updated_at)"
                    " VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
                    (run_id, spec.repository_id, position, spec_json,
                     json.dumps(list(spec.needs), separators=(",", ":")),
                     wm.RepositoryState.PENDING.value, now))
        except sqlite3.IntegrityError as error:
            raise RunAlreadyExists(
                "workspace run {0} already exists".format(run_id)) from error
        self._append_transition(run_id, None, "workspace", None, None,
                                "run-created", "coordinator", {}, now)
        return self._get_run(run_id)

    @serialized
    def get_run(self, run_id: str) -> WorkspaceRunRecord:
        return self._get_run(run_id)

    @serialized
    def list_runs(self) -> Tuple[WorkspaceRunRecord, ...]:
        rows = self.conn.execute(
            "SELECT run_id, workspace_id, workspace_digest, workspace_json, outcome,"
            " cancel_requested, created_at, lease_owner, lease_expires_at"
            " FROM workspace_runs ORDER BY run_id").fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    @serialized
    def get_repository(self, run_id: str, repository_id: str) -> RepositoryRecord:
        self._require_run(run_id)
        return self._get_repository(run_id, repository_id)

    @serialized
    def list_repositories(self, run_id: str) -> Tuple[RepositoryRecord, ...]:
        self._require_run(run_id)
        return tuple(self._repository_from_row(row) for row in self._repository_rows(run_id))

    @transactional_mutation
    def bind_repository_paths(
            self, run_id: str,
            repository_paths: Mapping[str, RepositoryPathBinding], *,
            lease_owner: Optional[str] = None) -> Tuple[RepositoryRecord, ...]:
        """Persist the exact canonical Git identity vector for a live run.

        Every member is stored atomically.  A migrated path-only binding has no
        authority to resume: it must fail closed rather than accepting a clone
        replacement at the same filesystem path.
        """
        self._require_lease_owner(run_id, lease_owner)
        if not isinstance(repository_paths, Mapping):
            raise RepositoryPathMismatch("repository paths must be a mapping")
        rows = self._repository_rows(run_id)
        declared_ids = {row["repository_id"] for row in rows}
        supplied_ids = set(repository_paths)
        if supplied_ids != declared_ids:
            raise RepositoryPathMismatch(
                "repository path mapping must name exactly the declared repositories")

        supplied = {}
        canonical_paths = {}
        for repository_id, binding in repository_paths.items():
            if not isinstance(repository_id, str) or not repository_id:
                raise RepositoryPathMismatch(
                    "repository path mapping keys must be nonempty strings")
            if not isinstance(binding, RepositoryPathBinding):
                raise RepositoryPathMismatch(
                    "repository path for {0} lacks a Git identity binding".format(
                        repository_id))
            if (not isinstance(binding.repository_identity, str)
                    or not binding.repository_identity):
                raise RepositoryPathMismatch(
                    "repository {0} has no immutable Git identity".format(
                        repository_id))
            try:
                canonical_path = Path(binding.resolved_path).resolve()
                canonical_common = Path(binding.git_common_dir).resolve()
            except (OSError, RuntimeError, TypeError) as error:
                raise RepositoryPathMismatch(
                    "repository binding for {0} cannot be resolved".format(
                        repository_id)) from error
            if not canonical_path.is_absolute() or not canonical_common.is_absolute():
                raise RepositoryPathMismatch(
                    "repository binding for {0} is not absolute".format(repository_id))
            for prior_id, prior_path in canonical_paths.items():
                try:
                    overlaps = (canonical_path == prior_path
                                or canonical_path.is_relative_to(prior_path)
                                or prior_path.is_relative_to(canonical_path))
                except AttributeError:
                    try:
                        canonical_path.relative_to(prior_path)
                        overlaps = True
                    except ValueError:
                        try:
                            prior_path.relative_to(canonical_path)
                            overlaps = True
                        except ValueError:
                            overlaps = canonical_path == prior_path
                if overlaps:
                    raise RepositoryPathMismatch(
                        "repository paths for {0} and {1} overlap".format(
                            prior_id, repository_id))
            canonical_paths[repository_id] = canonical_path
            supplied[repository_id] = RepositoryPathBinding(
                str(canonical_path), str(canonical_common),
                binding.repository_identity)

        persisted = {
            row["repository_id"]: RepositoryPathBinding(
                row["resolved_path"], row["git_common_dir"],
                row["repository_identity"])
            if (row["resolved_path"] is not None
                and row["git_common_dir"] is not None
                and row["repository_identity"] is not None)
            else None
            for row in rows
        }
        raw_persisted = {
            row["repository_id"]: (
                row["resolved_path"], row["git_common_dir"],
                row["repository_identity"])
            for row in rows
        }
        if any(row["identity_binding_state"] != 0 for row in rows):
            if (any(binding is None for binding in persisted.values())
                    or any(row["identity_binding_state"] != 1 for row in rows)):
                raise RepositoryPathMismatch(
                    "repository identity is absent from a migrated path binding")
        if all(all(value is None for value in values)
               for values in raw_persisted.values()):
            now = now_iso()
            for repository_id, binding in supplied.items():
                changed = self.conn.execute(
                    "UPDATE workspace_repositories SET resolved_path=?,"
                    " git_common_dir=?, repository_identity=?,"
                    " identity_binding_state=1, updated_at=?"
                    " WHERE run_id=? AND repository_id=?"
                    " AND resolved_path IS NULL AND git_common_dir IS NULL"
                    " AND repository_identity IS NULL AND identity_binding_state=0",
                    (binding.resolved_path, binding.git_common_dir,
                     binding.repository_identity, now, run_id,
                     repository_id)).rowcount
                if changed != 1:
                    raise RepositoryPathMismatch(
                        "repository identities changed while they were being bound")
            self._append_transition(
                run_id, None, "workspace", None, None, "repository-paths-bound",
                lease_owner,
                {"repository_paths": {
                    repository_id: binding.resolved_path
                    for repository_id, binding in supplied.items()},
                 "git_common_dirs": {
                    repository_id: binding.git_common_dir
                    for repository_id, binding in supplied.items()},
                 "repository_identities": {
                    repository_id: binding.repository_identity
                    for repository_id, binding in supplied.items()}},
                now)
        elif (any(binding is None for binding in persisted.values())
              or any(row["identity_binding_state"] != 1 for row in rows)):
            raise RepositoryPathMismatch(
                "repository identity is absent from a migrated path binding")
        elif persisted != supplied:
            raise RepositoryPathMismatch(
                "repository paths or immutable Git identities do not match this run")
        return tuple(self._repository_from_row(row)
                     for row in self._repository_rows(run_id))

    @transactional_mutation
    def claim_repository(self, run_id: str, repository_id: str, child_run_id: str,
                         candidate_branch: Optional[str], *,
                         lease_owner: Optional[str] = None) -> RepositoryRecord:
        """Atomically claim a single PENDING repository for a child run."""
        if not isinstance(child_run_id, str) or not child_run_id:
            raise ValueError("child_run_id must be a nonempty string")
        self._require_lease_owner(run_id, lease_owner)
        current = self._get_repository(run_id, repository_id)
        if current.state is not wm.RepositoryState.PENDING:
            raise IllegalTransition(
                "{0}/{1} is {2}, not PENDING".format(
                    run_id, repository_id, current.state.value))
        if (current.spec.mode is wm.RepositoryMode.WRITE
                and (not isinstance(candidate_branch, str) or not candidate_branch)):
            raise IllegalTransition("a writable repository claim requires a candidate branch")
        if candidate_branch is not None and (not isinstance(candidate_branch, str)
                                             or not candidate_branch):
            raise ValueError("candidate_branch must be nonempty when supplied")

        now = now_iso()
        changed = self.conn.execute(
            "UPDATE workspace_repositories SET state=?, child_run_id=?,"
            " candidate_branch=?, updated_at=?"
            " WHERE run_id=? AND repository_id=? AND state=?",
            (wm.RepositoryState.RUNNING.value, child_run_id, candidate_branch, now,
             run_id, repository_id, wm.RepositoryState.PENDING.value)).rowcount
        if changed != 1:
            raise IllegalTransition(
                "{0}/{1} was claimed concurrently".format(run_id, repository_id))
        self._append_transition(
            run_id, repository_id, "repository", wm.RepositoryState.PENDING.value,
            wm.RepositoryState.RUNNING.value, "claim", "coordinator",
            {"child_run_id": child_run_id, "candidate_branch": candidate_branch}, now)
        return self._get_repository(run_id, repository_id)

    @transactional_mutation
    def transition_repository(self, run_id: str, repository_id: str,
                              to_state: wm.RepositoryState, *,
                              accepted_sha: Optional[str] = None,
                              reason: Optional[str] = None,
                              actor: str = "coordinator",
                              lease_owner: Optional[str] = None) -> RepositoryRecord:
        """Apply a legal terminal repository transition and append its audit row."""
        self._require_lease_owner(run_id, lease_owner)
        current = self._get_repository(run_id, repository_id)
        if not isinstance(to_state, wm.RepositoryState):
            raise ValueError("to_state must be a RepositoryState")
        if to_state not in (wm.RepositoryState.ACCEPTED, wm.RepositoryState.BLOCKED,
                            wm.RepositoryState.CANCELLED):
            raise IllegalTransition("only terminal repository transitions use this method")
        allowed = {
            wm.RepositoryState.PENDING: (wm.RepositoryState.BLOCKED,
                                         wm.RepositoryState.CANCELLED),
            wm.RepositoryState.RUNNING: (wm.RepositoryState.ACCEPTED,
                                         wm.RepositoryState.BLOCKED,
                                         wm.RepositoryState.CANCELLED),
        }
        if to_state not in allowed.get(current.state, ()):
            raise IllegalTransition(
                "{0} cannot transition to {1}".format(
                    current.state.value, to_state.value))
        if to_state is wm.RepositoryState.ACCEPTED and not _is_sha(accepted_sha):
            raise IllegalTransition("ACCEPTED requires a 40- or 64-hex SHA")
        if accepted_sha is not None and not _is_sha(accepted_sha):
            raise ValueError("accepted_sha must be a 40- or 64-hex SHA")
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise ValueError("reason must be nonempty when supplied")

        transition_reason = reason or to_state.value.lower()
        block_reason = transition_reason if to_state is wm.RepositoryState.BLOCKED else None
        now = now_iso()
        changed = self.conn.execute(
            "UPDATE workspace_repositories SET state=?, accepted_sha=?,"
            " block_reason=?, updated_at=?"
            " WHERE run_id=? AND repository_id=? AND state=?",
            (to_state.value, accepted_sha if to_state is wm.RepositoryState.ACCEPTED else None,
             block_reason, now, run_id, repository_id, current.state.value)).rowcount
        if changed != 1:
            raise IllegalTransition(
                "{0}/{1} changed concurrently".format(run_id, repository_id))
        detail = {}
        if accepted_sha is not None:
            detail["accepted_sha"] = accepted_sha
        self._append_transition(run_id, repository_id, "repository",
                                current.state.value, to_state.value,
                                transition_reason, actor, detail, now)
        return self._get_repository(run_id, repository_id)

    @transactional_mutation
    def block_pending_descendants(self, run_id: str, *,
                                  lease_owner: Optional[str] = None) -> Tuple[str, ...]:
        """Persist BLOCKED for every pending descendant of a blocked repository."""
        self._require_lease_owner(run_id, lease_owner)
        rows = self._repository_rows(run_id)
        state_by_id = {row["repository_id"]: wm.RepositoryState(row["state"])
                       for row in rows}
        needs_by_id = {row["repository_id"]: tuple(json.loads(row["needs_json"]))
                       for row in rows}
        blocked = {repository_id for repository_id, state in state_by_id.items()
                   if state in (wm.RepositoryState.BLOCKED, wm.RepositoryState.CANCELLED)}
        descendants = []
        changed = True
        while changed:
            changed = False
            for row in rows:
                repository_id = row["repository_id"]
                if state_by_id[repository_id] is not wm.RepositoryState.PENDING:
                    continue
                blocker = next((need for need in needs_by_id[repository_id]
                                if need in blocked), None)
                if blocker is None:
                    continue
                state_by_id[repository_id] = wm.RepositoryState.BLOCKED
                blocked.add(repository_id)
                descendants.append((repository_id, blocker))
                changed = True
        if not descendants:
            return ()

        now = now_iso()
        for repository_id, blocker in descendants:
            changed_rows = self.conn.execute(
                "UPDATE workspace_repositories SET state=?, block_reason=?, updated_at=?"
                " WHERE run_id=? AND repository_id=? AND state=?",
                (wm.RepositoryState.BLOCKED.value, "upstream-blocked:{0}".format(blocker),
                 now, run_id, repository_id, wm.RepositoryState.PENDING.value)).rowcount
            if changed_rows != 1:
                raise IllegalTransition(
                    "{0}/{1} changed while descendants were blocked".format(
                        run_id, repository_id))
            self._append_transition(
                run_id, repository_id, "repository", wm.RepositoryState.PENDING.value,
                wm.RepositoryState.BLOCKED.value, "upstream-blocked", "coordinator",
                {"blocked_by": blocker}, now)
        return tuple(repository_id for repository_id, _ in descendants)

    # ── cancellation and integration gates ────────────────────────────────

    @transactional_mutation
    def request_cancellation(self, run_id: str, *, actor: str = "operator"
                             ) -> WorkspaceRunRecord:
        """Record a durable cancellation request without inventing repo outcomes."""
        run = self._require_run(run_id)
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        if bool(run["cancel_requested"]):
            return self._run_from_row(run)
        now = now_iso()
        self.conn.execute(
            "UPDATE workspace_runs SET cancel_requested=1 WHERE run_id=?", (run_id,))
        self._append_transition(run_id, None, "workspace", None, None,
                                "cancellation-requested", actor, {}, now)
        return self._get_run(run_id)

    @serialized
    def cancellation_requested(self, run_id: str) -> bool:
        return bool(self._require_run(run_id)["cancel_requested"])

    @transactional_mutation
    def record_gate(self, run_id: str, gate_index: int, *, passed: bool,
                    detail: Optional[Mapping[str, Any]] = None,
                    actor: str = "coordinator",
                    lease_owner: Optional[str] = None) -> GateRecord:
        """Record one immutable result for a declared workspace integration gate."""
        run = self._require_lease_owner(run_id, lease_owner)
        workspace = self._workspace_from_json(run["workspace_json"])
        if not isinstance(gate_index, int) or isinstance(gate_index, bool):
            raise ValueError("gate_index must be an integer")
        if gate_index < 0 or gate_index >= len(workspace.integration_gates):
            raise ValueError("gate_index does not name a declared integration gate")
        if not isinstance(passed, bool):
            raise ValueError("passed must be a bool")
        if detail is not None and not isinstance(detail, Mapping):
            raise ValueError("detail must be a mapping")
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        existing = self.conn.execute(
            "SELECT 1 FROM workspace_gates WHERE run_id=? AND gate_index=?",
            (run_id, gate_index)).fetchone()
        if existing is not None:
            raise GateAlreadyRecorded(
                "gate {0} already has an authoritative result".format(gate_index))

        now = now_iso()
        stored_detail = _json(detail or {})
        try:
            self.conn.execute(
                "INSERT INTO workspace_gates (run_id, gate_index, passed, detail_json,"
                " recorded_at) VALUES (?,?,?,?,?)",
                (run_id, gate_index, int(passed), stored_detail, now))
        except sqlite3.IntegrityError as error:
            raise GateAlreadyRecorded(
                "gate {0} already has an authoritative result".format(gate_index)) from error
        audit_detail = {**dict(detail or {}), "gate_index": gate_index}
        self._append_transition(
            run_id, None, "gate", None, "PASSED" if passed else "FAILED",
            "gate-recorded", actor, audit_detail, now)
        return self._get_gate(run_id, gate_index)

    @serialized
    def list_gates(self, run_id: str) -> Tuple[GateRecord, ...]:
        self._require_run(run_id)
        rows = self.conn.execute(
            "SELECT run_id, gate_index, passed, detail_json, recorded_at"
            " FROM workspace_gates WHERE run_id=? ORDER BY gate_index", (run_id,)).fetchall()
        return tuple(self._gate_from_row(row) for row in rows)

    # ── outcome / publication ─────────────────────────────────────────────

    @transactional_mutation
    def declare_outcome(self, run_id: str, outcome: wm.WorkspaceOutcome, *,
                        actor: str = "coordinator",
                        lease_owner: Optional[str] = None) -> WorkspaceRunRecord:
        """Declare one legal workspace outcome, preserving the prior state in audit."""
        run = self._require_run(run_id)
        if not isinstance(outcome, wm.WorkspaceOutcome):
            raise ValueError("outcome must be a WorkspaceOutcome")
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        current = _enum_or_none(wm.WorkspaceOutcome, run["outcome"])
        if current is None:
            self._require_lease_owner(run_id, lease_owner)
        self._guard_outcome(run_id, current, outcome)

        now = now_iso()
        self.conn.execute(
            "UPDATE workspace_runs SET outcome=? WHERE run_id=?",
            (outcome.value, run_id))
        self._append_transition(run_id, None, "workspace",
                                current.value if current else None, outcome.value,
                                "outcome-declared", actor, {}, now)
        return self._get_run(run_id)

    @transactional_mutation
    def prepare_publication(
            self, run_id: str, *,
            remote_identities: Mapping[str, PullRequestRemoteIdentity],
            lease_owner: str, lease_now: float,
            actor: str = "coordinator") -> PublicationIntentRecord:
        """Create once the ordered writable-repository publication target vector."""
        run = self._require_live_lease_owner(run_id, lease_owner, lease_now)
        workspace = self._workspace_from_json(run["workspace_json"])
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        if not isinstance(remote_identities, Mapping):
            raise ValueError("remote_identities must be a mapping")
        if _enum_or_none(wm.WorkspaceOutcome, run["outcome"]) is not wm.WorkspaceOutcome.ACCEPTED:
            raise PublicationRefused("publication requires an ACCEPTED workspace outcome")
        if workspace.publication_mode is wm.PublicationMode.NONE:
            raise PublicationRefused("this workspace has no publication mode")
        existing = self.conn.execute(
            "SELECT 1 FROM publication_intents WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None:
            raise DuplicatePublicationIntent(
                "publication intent for {0} already exists".format(run_id))
        if not self._all_declared_gates_passed(run_id, workspace):
            raise PublicationRefused("publication requires every integration gate to pass")

        repository_rows = self._repository_rows(run_id)
        targets = []
        for row in repository_rows:
            spec = self._spec_from_json(row["spec_json"])
            if spec.mode is not wm.RepositoryMode.WRITE:
                continue
            if wm.RepositoryState(row["state"]) is not wm.RepositoryState.ACCEPTED:
                raise PublicationRefused(
                    "writable repository {0} is not ACCEPTED".format(spec.repository_id))
            if not row["candidate_branch"] or not _is_sha(row["accepted_sha"]):
                raise PublicationRefused(
                    "writable repository {0} lacks publication recovery data".format(
                        spec.repository_id))
            if not spec.target_branch:
                raise PublicationRefused(
                    "writable repository {0} has no target branch".format(spec.repository_id))
            targets.append((row, spec))
        if not targets:
            raise PublicationRefused("publication requires at least one writable repository")

        writable_ids = {spec.repository_id for _, spec in targets}
        if workspace.publication_mode is wm.PublicationMode.PULL_REQUESTS:
            if set(remote_identities) != writable_ids:
                raise PublicationRefused(
                    "pull-request publication needs every resolved remote identity")
            for repository_id, identity in remote_identities.items():
                if (not isinstance(identity, PullRequestRemoteIdentity)
                        or not isinstance(identity.remote_url, str)
                        or not identity.remote_url
                        or not isinstance(identity.remote_repository, str)
                        or not identity.remote_repository):
                    raise PublicationRefused(
                        "pull-request remote identity is invalid for {0}".format(
                            repository_id))
        elif remote_identities:
            raise PublicationRefused(
                "local-ref publication must not persist pull-request remote identities")

        now = now_iso()
        try:
            self.conn.execute(
                "INSERT INTO publication_intents (run_id, state, prepared_at, updated_at)"
                " VALUES (?,?,?,?)",
                (run_id, wm.PublicationState.PREPARED.value, now, now))
        except sqlite3.IntegrityError as error:
            raise DuplicatePublicationIntent(
                "publication intent for {0} already exists".format(run_id)) from error
        for position, (row, spec) in enumerate(targets):
            identity = remote_identities.get(spec.repository_id)
            self.conn.execute(
                "INSERT INTO publication_targets (run_id, position, repository_id,"
                " expected_base_sha, target_branch, candidate_branch, accepted_sha,"
                " remote_url, remote_repository, state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, position, spec.repository_id, spec.base_commit,
                 spec.target_branch, row["candidate_branch"], row["accepted_sha"],
                 identity.remote_url if identity is not None else None,
                 identity.remote_repository if identity is not None else None,
                 wm.PublicationState.PENDING.value))
        self._append_transition(
            run_id, None, "publication", wm.PublicationState.PENDING.value,
            wm.PublicationState.PREPARED.value, "publication-prepared", actor,
            {"repository_ids": [spec.repository_id for _, spec in targets],
             "remote_identities": {
                 repository_id: {
                     "remote_url": identity.remote_url,
                     "remote_repository": identity.remote_repository}
                 for repository_id, identity in remote_identities.items()}},
            now)
        return self._get_publication_intent(run_id)

    @serialized
    def get_publication_intent(self, run_id: str) -> PublicationIntentRecord:
        self._require_run(run_id)
        return self._get_publication_intent(run_id)

    @transactional_mutation
    def record_publication_step(self, run_id: str, repository_id: str,
                                to_state: wm.PublicationState, *,
                                lease_owner: str, lease_now: float,
                                detail: Optional[Mapping[str, Any]] = None,
                                actor: str = "coordinator") -> PublicationStepRecord:
        """Append and apply one legal publication-target state transition."""
        run = self._require_live_lease_owner(run_id, lease_owner, lease_now)
        if _enum_or_none(wm.WorkspaceOutcome, run["outcome"]) is not wm.WorkspaceOutcome.ACCEPTED:
            raise PublicationRefused("publication steps require an ACCEPTED workspace outcome")
        if not isinstance(to_state, wm.PublicationState):
            raise ValueError("to_state must be a PublicationState")
        if detail is not None and not isinstance(detail, Mapping):
            raise ValueError("detail must be a mapping")
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        intent = self._publication_intent_row(run_id)
        if intent is None:
            raise PublicationRefused("publication has not been prepared")
        intent_state = wm.PublicationState(intent["state"])
        target = self.conn.execute(
            "SELECT run_id, position, repository_id, expected_base_sha, target_branch,"
            " candidate_branch, accepted_sha, state FROM publication_targets"
            " WHERE run_id=? AND repository_id=?", (run_id, repository_id)).fetchone()
        if target is None:
            if self.conn.execute(
                    "SELECT 1 FROM workspace_repositories WHERE run_id=? AND repository_id=?",
                    (run_id, repository_id)).fetchone() is None:
                raise UnknownRepository(
                    "workspace run {0} has no repository {1}".format(run_id, repository_id))
            raise PublicationRefused(
                "repository {0} is not in the publication target vector".format(repository_id))
        current = wm.PublicationState(target["state"])
        self._guard_publication_step(intent_state, current, to_state)

        now = now_iso()
        stored_detail = _json(detail or {})
        changed = self.conn.execute(
            "UPDATE publication_targets SET state=?"
            " WHERE run_id=? AND repository_id=? AND state=?",
            (to_state.value, run_id, repository_id, current.value)).rowcount
        if changed != 1:
            raise PublicationRefused(
                "publication target {0} changed concurrently".format(repository_id))
        cursor = self.conn.execute(
            "INSERT INTO publication_steps (run_id, repository_id, from_state, to_state,"
            " detail_json, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, repository_id, current.value, to_state.value, stored_detail, now))
        next_intent_state = self._publication_intent_state(run_id, intent_state)
        self.conn.execute(
            "UPDATE publication_intents SET state=?, updated_at=? WHERE run_id=?",
            (next_intent_state.value, now, run_id))
        self._append_transition(
            run_id, repository_id, "publication", current.value, to_state.value,
            "publication-step", actor, dict(detail or {}), now)
        row = self.conn.execute(
            "SELECT id, run_id, repository_id, from_state, to_state, detail_json, created_at"
            " FROM publication_steps WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._publication_step_from_row(row)

    @transactional_mutation
    def record_unexpected_prepared_target_failure(
            self, run_id: str, repository_id: str, *, detail: Mapping[str, Any],
            lease_owner: str, lease_now: float,
            actor: str = "coordinator") -> PublicationStepRecord:
        """Atomically fail a corrupt target that was persisted as PREPARED.

        A normal publication target is created PENDING.  PREPARED is the intent
        state, never a legal target state.  This narrowly scoped recovery
        transition makes that corruption durable before the publisher changes
        the workspace outcome to manual recovery.
        """
        run = self._require_live_lease_owner(run_id, lease_owner, lease_now)
        if _enum_or_none(wm.WorkspaceOutcome, run["outcome"]) is not wm.WorkspaceOutcome.ACCEPTED:
            raise PublicationRefused("publication steps require an ACCEPTED workspace outcome")
        if not isinstance(detail, Mapping):
            raise ValueError("detail must be a mapping")
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be a nonempty string")
        intent = self._publication_intent_row(run_id)
        if intent is None:
            raise PublicationRefused("publication has not been prepared")
        if wm.PublicationState(intent["state"]) is not wm.PublicationState.PREPARED:
            raise PublicationRefused(
                "unexpected prepared target requires a prepared publication intent")
        target = self.conn.execute(
            "SELECT state FROM publication_targets"
            " WHERE run_id=? AND repository_id=?", (run_id, repository_id)).fetchone()
        if target is None:
            raise PublicationRefused(
                "repository {0} is not in the publication target vector".format(repository_id))
        if wm.PublicationState(target["state"]) is not wm.PublicationState.PREPARED:
            raise PublicationRefused(
                "publication target {0} is not unexpectedly prepared".format(
                    repository_id))

        now = now_iso()
        stored_detail = _json(dict(detail))
        changed = self.conn.execute(
            "UPDATE publication_targets SET state=?"
            " WHERE run_id=? AND repository_id=? AND state=?",
            (wm.PublicationState.FAILED.value, run_id, repository_id,
             wm.PublicationState.PREPARED.value)).rowcount
        if changed != 1:
            raise PublicationRefused(
                "publication target {0} changed concurrently".format(repository_id))
        cursor = self.conn.execute(
            "INSERT INTO publication_steps (run_id, repository_id, from_state, to_state,"
            " detail_json, created_at) VALUES (?,?,?,?,?,?)",
            (run_id, repository_id, wm.PublicationState.PREPARED.value,
             wm.PublicationState.FAILED.value, stored_detail, now))
        self.conn.execute(
            "UPDATE publication_intents SET state=?, updated_at=? WHERE run_id=?",
            (wm.PublicationState.FAILED.value, now, run_id))
        self._append_transition(
            run_id, repository_id, "publication", wm.PublicationState.PREPARED.value,
            wm.PublicationState.FAILED.value,
            "publication-unexpected-prepared-target", actor, dict(detail), now)
        row = self.conn.execute(
            "SELECT id, run_id, repository_id, from_state, to_state, detail_json, created_at"
            " FROM publication_steps WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._publication_step_from_row(row)

    @serialized
    def list_publication_steps(self, run_id: str) -> Tuple[PublicationStepRecord, ...]:
        self._require_run(run_id)
        rows = self.conn.execute(
            "SELECT id, run_id, repository_id, from_state, to_state, detail_json, created_at"
            " FROM publication_steps WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return tuple(self._publication_step_from_row(row) for row in rows)

    # ── audit and lease ───────────────────────────────────────────────────

    @serialized
    def audit_transitions(self, run_id: str) -> Tuple[TransitionRecord, ...]:
        self._require_run(run_id)
        rows = self.conn.execute(
            "SELECT id, run_id, repository_id, kind, from_state, to_state, reason, actor,"
            " detail_json, created_at FROM coordinator_transitions"
            " WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return tuple(self._transition_from_row(row) for row in rows)

    @transactional_mutation
    def acquire_lease(self, run_id: str, owner: str, now: float,
                      stale_after_s: float) -> bool:
        """Acquire or renew a lease when it is ours, absent, or already expired."""
        self._validate_lease_args(owner, now, stale_after_s)
        row = self._require_run(run_id)
        current_owner = row["lease_owner"]
        expires_at = row["lease_expires_at"]
        if (current_owner is not None and current_owner != owner
                and expires_at is not None and float(expires_at) > float(now)):
            return False

        expiry = float(now) + float(stale_after_s)
        stamp = now_iso()
        changed = self.conn.execute(
            "UPDATE workspace_runs SET lease_owner=?, lease_expires_at=?"
            " WHERE run_id=? AND (lease_owner IS NULL OR lease_owner=?"
            " OR lease_expires_at IS NULL OR lease_expires_at<=?)",
            (owner, expiry, run_id, owner, float(now))).rowcount
        if changed != 1:
            return False
        self._append_transition(
            run_id, None, "lease", None, None, "lease-acquired", owner,
            {"owner": owner, "expires_at": expiry}, stamp)
        return True

    @transactional_mutation
    def heartbeat_lease(self, run_id: str, owner: str, now: float,
                        stale_after_s: float) -> bool:
        """Extend an unexpired lease only for its current owner."""
        self._validate_lease_args(owner, now, stale_after_s)
        self._require_run(run_id)
        expiry = float(now) + float(stale_after_s)
        changed = self.conn.execute(
            "UPDATE workspace_runs SET lease_expires_at=?"
            " WHERE run_id=? AND lease_owner=? AND lease_expires_at>?",
            (expiry, run_id, owner, float(now))).rowcount
        if changed != 1:
            return False
        return True

    @transactional_mutation
    def release_lease(self, run_id: str, owner: str) -> bool:
        """Release a lease only when the caller remains its recorded owner."""
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner must be a nonempty string")
        self._require_run(run_id)
        stamp = now_iso()
        changed = self.conn.execute(
            "UPDATE workspace_runs SET lease_owner=NULL, lease_expires_at=NULL"
            " WHERE run_id=? AND lease_owner=?", (run_id, owner)).rowcount
        if changed != 1:
            return False
        self._append_transition(run_id, None, "lease", None, None,
                                "lease-released", owner, {"owner": owner}, stamp)
        return True

    def close(self) -> None:
        """Close the one shared SQLite connection explicitly."""
        with self._lock:
            self.conn.close()

    # ── private authority readers ─────────────────────────────────────────

    def _require_run(self, run_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT run_id, workspace_id, workspace_digest, workspace_json, outcome,"
            " cancel_requested, created_at, lease_owner, lease_expires_at"
            " FROM workspace_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRun("workspace run {0} does not exist".format(run_id))
        return row

    def _require_lease_owner(self, run_id: str,
                             lease_owner: Optional[str]) -> sqlite3.Row:
        """Return a run only when this transaction still owns its lease."""
        run = self._require_run(run_id)
        if (not isinstance(lease_owner, str) or not lease_owner
                or run["lease_owner"] != lease_owner):
            raise LeaseOwnershipError(
                "workspace run {0} is not leased by the supplied owner".format(run_id))
        return run

    def _require_live_lease_owner(self, run_id: str, lease_owner: str,
                                  lease_now: float) -> sqlite3.Row:
        """Return a run only when ``lease_owner`` holds an unexpired lease.

        Publication mutations receive their observed lease time explicitly so
        deterministic callers can use the same clock as acquisition.  The
        check shares the lease expiry boundary used by ``acquire_lease``.
        """
        self._validate_lease_args(lease_owner, lease_now, 1.0)
        run = self._require_run(run_id)
        expiry = run["lease_expires_at"]
        if (run["lease_owner"] != lease_owner or expiry is None
                or float(expiry) <= float(lease_now)):
            raise LeaseOwnershipError(
                "workspace run {0} has no live lease for the supplied owner".format(
                    run_id))
        return run

    def _get_run(self, run_id: str) -> WorkspaceRunRecord:
        return self._run_from_row(self._require_run(run_id))

    @staticmethod
    def _workspace_from_json(stored: str) -> wm.WorkspacePlan:
        return wm.WorkspacePlan.model_validate(json.loads(stored))

    @staticmethod
    def _spec_from_json(stored: str) -> wm.RepositorySpec:
        return wm.RepositorySpec.model_validate(json.loads(stored))

    def _run_from_row(self, row: sqlite3.Row) -> WorkspaceRunRecord:
        return WorkspaceRunRecord(
            run_id=row["run_id"], workspace_id=row["workspace_id"],
            workspace_digest=row["workspace_digest"],
            workspace=self._workspace_from_json(row["workspace_json"]),
            outcome=_enum_or_none(wm.WorkspaceOutcome, row["outcome"]),
            cancel_requested=bool(row["cancel_requested"]), created_at=row["created_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=(float(row["lease_expires_at"])
                              if row["lease_expires_at"] is not None else None))

    def _repository_identity_projection(self) -> str:
        if self._has_repository_identity_columns:
            return (" resolved_path, git_common_dir, repository_identity,"
                    " identity_binding_state,")
        # Read-only legacy access remains observational; mutation refuses the
        # missing authority rather than treating it as a fresh binding.
        return (" NULL AS resolved_path, NULL AS git_common_dir,"
                " NULL AS repository_identity, -1 AS identity_binding_state,")

    def _repository_rows(self, run_id: str) -> Tuple[sqlite3.Row, ...]:
        return tuple(self.conn.execute(
            "SELECT run_id, repository_id, position, spec_json, needs_json, state,"
            " child_run_id, candidate_branch, accepted_sha, block_reason,"
            + self._repository_identity_projection() +
            " updated_at FROM workspace_repositories WHERE run_id=? ORDER BY position",
            (run_id,)).fetchall())

    def _get_repository(self, run_id: str, repository_id: str) -> RepositoryRecord:
        row = self.conn.execute(
            "SELECT run_id, repository_id, position, spec_json, needs_json, state,"
            " child_run_id, candidate_branch, accepted_sha, block_reason,"
            + self._repository_identity_projection() +
            " updated_at FROM workspace_repositories"
            " WHERE run_id=? AND repository_id=?",
            (run_id, repository_id)).fetchone()
        if row is None:
            raise UnknownRepository(
                "workspace run {0} has no repository {1}".format(run_id, repository_id))
        return self._repository_from_row(row)

    def _repository_from_row(self, row: sqlite3.Row) -> RepositoryRecord:
        return RepositoryRecord(
            run_id=row["run_id"], repository_id=row["repository_id"],
            position=row["position"], spec=self._spec_from_json(row["spec_json"]),
            state=wm.RepositoryState(row["state"]), child_run_id=row["child_run_id"],
            candidate_branch=row["candidate_branch"], accepted_sha=row["accepted_sha"],
            resolved_path=row["resolved_path"],
            git_common_dir=row["git_common_dir"],
            repository_identity=row["repository_identity"],
            block_reason=row["block_reason"], updated_at=row["updated_at"])

    def _get_gate(self, run_id: str, gate_index: int) -> GateRecord:
        row = self.conn.execute(
            "SELECT run_id, gate_index, passed, detail_json, recorded_at"
            " FROM workspace_gates WHERE run_id=? AND gate_index=?",
            (run_id, gate_index)).fetchone()
        if row is None:
            raise CoordinatorStoreError(
                "workspace gate {0} has no recorded result".format(gate_index))
        return self._gate_from_row(row)

    @staticmethod
    def _gate_from_row(row: sqlite3.Row) -> GateRecord:
        return GateRecord(
            run_id=row["run_id"], gate_index=row["gate_index"],
            passed=bool(row["passed"]), detail=_decode_mapping(row["detail_json"]),
            recorded_at=row["recorded_at"])

    def _publication_intent_row(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT run_id, state, prepared_at, updated_at FROM publication_intents"
            " WHERE run_id=?", (run_id,)).fetchone()

    def _get_publication_intent(self, run_id: str) -> PublicationIntentRecord:
        row = self._publication_intent_row(run_id)
        if row is None:
            raise PublicationRefused("workspace run {0} has no publication intent".format(run_id))
        targets = self.conn.execute(
            "SELECT repository_id, expected_base_sha, target_branch, candidate_branch,"
            " accepted_sha, remote_url, remote_repository, state"
            " FROM publication_targets WHERE run_id=? ORDER BY position",
            (run_id,)).fetchall()
        return PublicationIntentRecord(
            run_id=row["run_id"], state=wm.PublicationState(row["state"]),
            targets=tuple(PublicationTarget(
                repository_id=target["repository_id"],
                expected_base_sha=target["expected_base_sha"],
                target_branch=target["target_branch"],
                candidate_branch=target["candidate_branch"],
                accepted_sha=target["accepted_sha"],
                remote_url=target["remote_url"],
                remote_repository=target["remote_repository"],
                state=wm.PublicationState(target["state"])) for target in targets),
            prepared_at=row["prepared_at"], updated_at=row["updated_at"])

    @staticmethod
    def _publication_step_from_row(row: sqlite3.Row) -> PublicationStepRecord:
        return PublicationStepRecord(
            step_id=row["id"], run_id=row["run_id"], repository_id=row["repository_id"],
            from_state=wm.PublicationState(row["from_state"]),
            to_state=wm.PublicationState(row["to_state"]),
            detail=_decode_mapping(row["detail_json"]), created_at=row["created_at"])

    @staticmethod
    def _transition_from_row(row: sqlite3.Row) -> TransitionRecord:
        kind = row["kind"]
        from_state = row["from_state"]
        to_state = row["to_state"]
        if kind == "repository":
            from_state = _enum_or_none(wm.RepositoryState, from_state)
            to_state = _enum_or_none(wm.RepositoryState, to_state)
        elif kind == "publication":
            from_state = _enum_or_none(wm.PublicationState, from_state)
            to_state = _enum_or_none(wm.PublicationState, to_state)
        elif kind == "workspace":
            from_state = _enum_or_none(wm.WorkspaceOutcome, from_state)
            to_state = _enum_or_none(wm.WorkspaceOutcome, to_state)
        return TransitionRecord(
            transition_id=row["id"], run_id=row["run_id"],
            repository_id=row["repository_id"], kind=kind,
            from_state=from_state, to_state=to_state, reason=row["reason"],
            actor=row["actor"], detail=_decode_mapping(row["detail_json"]),
            created_at=row["created_at"])

    # ── private guards and append helpers ──────────────────────────────────

    def _append_transition(self, run_id: str, repository_id: Optional[str], kind: str,
                           from_state: Optional[str], to_state: Optional[str],
                           reason: str, actor: str, detail: Mapping[str, Any],
                           created_at: str) -> None:
        self.conn.execute(
            "INSERT INTO coordinator_transitions (run_id, repository_id, kind, from_state,"
            " to_state, reason, actor, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, repository_id, kind, from_state, to_state, reason, actor,
             _json(detail), created_at))

    def _all_declared_gates_passed(self, run_id: str,
                                   workspace: wm.WorkspacePlan) -> bool:
        rows = self.conn.execute(
            "SELECT gate_index, passed FROM workspace_gates WHERE run_id=?", (run_id,)).fetchall()
        results = {row["gate_index"]: bool(row["passed"]) for row in rows}
        return (len(results) == len(workspace.integration_gates)
                and all(results.get(index) is True
                        for index in range(len(workspace.integration_gates))))

    def _guard_outcome(self, run_id: str, current: Optional[wm.WorkspaceOutcome],
                       target: wm.WorkspaceOutcome) -> None:
        repositories = self._repository_rows(run_id)
        if current is None:
            if target is wm.WorkspaceOutcome.ACCEPTED:
                if any(wm.RepositoryState(row["state"]) is not wm.RepositoryState.ACCEPTED
                       for row in repositories):
                    raise IllegalTransition("ACCEPTED requires every repository to be ACCEPTED")
                workspace = self._get_run(run_id).workspace
                if not self._all_declared_gates_passed(run_id, workspace):
                    raise IllegalTransition("ACCEPTED requires every integration gate to pass")
                return
            if target is wm.WorkspaceOutcome.BLOCKED:
                blocked = any(wm.RepositoryState(row["state"]) is wm.RepositoryState.BLOCKED
                              for row in repositories)
                failed_gate = any(not record.passed for record in self.list_gates(run_id))
                if not blocked and not failed_gate:
                    raise IllegalTransition("BLOCKED requires a blocked repository or failed gate")
                return
            if target is wm.WorkspaceOutcome.CANCELLED:
                cancelled = all(wm.RepositoryState(row["state"]) is wm.RepositoryState.CANCELLED
                                for row in repositories)
                if not bool(self._require_run(run_id)["cancel_requested"]) and not cancelled:
                    raise IllegalTransition("CANCELLED requires a cancellation request or all cancelled")
                return
            if target is wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED:
                return
            raise IllegalTransition("{0} requires a prior ACCEPTED outcome".format(target.value))
        if current is wm.WorkspaceOutcome.ACCEPTED:
            if target is wm.WorkspaceOutcome.PUBLISHED:
                intent = self._publication_intent_row(run_id)
                if intent is None or wm.PublicationState(intent["state"]) is not wm.PublicationState.PUBLISHED:
                    raise IllegalTransition("PUBLISHED requires a fully published intent")
                return
            if target is wm.WorkspaceOutcome.PARTIALLY_PUBLISHED:
                intent = self._publication_intent_row(run_id)
                if intent is None or wm.PublicationState(intent["state"]) is not wm.PublicationState.FAILED:
                    raise IllegalTransition("PARTIALLY_PUBLISHED requires a failed publication intent")
                return
            if target is wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED:
                intent = self._publication_intent_row(run_id)
                if intent is None or wm.PublicationState(intent["state"]) not in (
                        wm.PublicationState.FAILED, wm.PublicationState.ROLLED_BACK):
                    raise IllegalTransition("manual recovery requires a failed publication intent")
                return
        if (current is wm.WorkspaceOutcome.PARTIALLY_PUBLISHED
                and target is wm.WorkspaceOutcome.MANUAL_RECOVERY_REQUIRED):
            return
        raise IllegalTransition(
            "workspace outcome {0} cannot transition to {1}".format(
                current.value, target.value))

    @staticmethod
    def _guard_publication_step(intent_state: wm.PublicationState,
                                current: wm.PublicationState,
                                target: wm.PublicationState) -> None:
        if intent_state is wm.PublicationState.PREPARED:
            if (current is wm.PublicationState.PENDING
                    and target in (wm.PublicationState.PUBLISHED,
                                   wm.PublicationState.FAILED)):
                return
        elif intent_state is wm.PublicationState.FAILED:
            if (current in (wm.PublicationState.PENDING, wm.PublicationState.PUBLISHED,
                            wm.PublicationState.FAILED)
                    and target is wm.PublicationState.ROLLED_BACK):
                return
        raise PublicationRefused(
            "publication state {0} cannot move target {1} to {2}".format(
                intent_state.value, current.value, target.value))

    def _publication_intent_state(self, run_id: str,
                                  current: wm.PublicationState) -> wm.PublicationState:
        states = tuple(wm.PublicationState(row["state"]) for row in self.conn.execute(
            "SELECT state FROM publication_targets WHERE run_id=? ORDER BY position",
            (run_id,)).fetchall())
        if states and all(state is wm.PublicationState.PUBLISHED for state in states):
            return wm.PublicationState.PUBLISHED
        if any(state is wm.PublicationState.FAILED for state in states):
            return wm.PublicationState.FAILED
        if current is wm.PublicationState.FAILED:
            if states and all(state is wm.PublicationState.ROLLED_BACK for state in states):
                return wm.PublicationState.ROLLED_BACK
            return wm.PublicationState.FAILED
        return wm.PublicationState.PREPARED

    @staticmethod
    def _validate_lease_args(owner: str, now: float, stale_after_s: float) -> None:
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner must be a nonempty string")
        if (not isinstance(now, (int, float)) or isinstance(now, bool)
                or not math.isfinite(float(now))):
            raise ValueError("now must be finite numeric")
        if (not isinstance(stale_after_s, (int, float)) or isinstance(stale_after_s, bool)
                or not math.isfinite(float(stale_after_s))):
            raise ValueError("stale_after_s must be finite numeric")
        if float(stale_after_s) <= 0:
            raise ValueError("stale_after_s must be greater than zero")
