"""Deterministic runtime mechanics for one multi-repository Maestro workspace.

This module owns filesystem and Git state only.  The coordinator owns durable
state transitions, while every child run remains responsible for its own
single-repository scheduler and agent panes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from . import coordinator_store as cs
from . import plan_digest
from . import plan_model as pm
from . import runner_resolution as rr
from . import workspace_model as wm
from . import worktree as wt


_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MANIFEST_SCHEMA = "maestro-acceptance.v1"


class WorkspaceRuntimeError(RuntimeError):
    """A runtime fact that refuses workspace execution."""


class RepositoryPathError(WorkspaceRuntimeError):
    """A declared repository path is unsafe, absent, overlapping, or not Git."""



class GitEnvironmentalError(RepositoryPathError):
    """A Git command failed outside a documented absence/negative contract."""

class RepositoryBaseError(WorkspaceRuntimeError):
    """The exact declared base is not a commit in its declared repository."""


class PlanBindingError(WorkspaceRuntimeError):
    """A writer's stored plan does not bind to its workspace declaration."""


class CandidatePreparationError(WorkspaceRuntimeError):
    """A candidate branch/worktree cannot be created or safely resumed."""


class AcceptedResultError(WorkspaceRuntimeError):
    """A child's accepted result does not name its candidate's valid head."""


class AcceptanceError(WorkspaceRuntimeError):
    """The detached cross-repository acceptance checkout cannot be assembled."""


class GateConfigurationError(WorkspaceRuntimeError):
    """A global gate's acceptance-relative working directory is unsafe."""


class CleanupError(WorkspaceRuntimeError):
    """An acceptance worktree could not be removed without touching candidates."""


@dataclass(frozen=True)
class CandidateRepository:
    """The one deterministic candidate identity for a writable participant."""

    repository_id: str
    repository_path: Path
    base_commit: str
    candidate_branch: str
    candidate_worktree: Path


@dataclass(frozen=True)
class AcceptanceWorkspace:
    """Detached, read-only repository trees used exclusively by global gates."""

    run_id: str
    root: Path
    repository_paths: Mapping[str, Path]
    repository_shas: Mapping[str, str]
    manifest_path: Path
    source_paths: Mapping[str, Path]


class GateFailure(WorkspaceRuntimeError):
    """A declared global gate was non-green or reported too few passed cases."""

    def __init__(self, gate_index: int, result: wt.GateResult,
                 completed: Sequence[wt.GateResult] = ()) -> None:
        self.gate_index = gate_index
        self.result = result
        self.completed = tuple(completed)
        super().__init__(
            "global gate {0} failed: exit={1}, passed={2}".format(
                gate_index, result.exit_code, result.counts.get("passed", 0)))


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one explicit Git command without accepting a shell or implicit cwd."""
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True)
    except (OSError, ValueError) as exc:
        raise GitEnvironmentalError(
            "could not run git {0} in {1}: {2}".format(
                " ".join(args), cwd, exc)) from exc


def _git_error(cwd: Path, args: Sequence[str], result: subprocess.CompletedProcess) -> str:
    detail = (result.stderr or result.stdout or "Git rejected the command").strip()
    return "git {0} in {1}: {2}".format(" ".join(args), cwd, detail)


def _unexpected_git_failure(cwd: Path, args: Sequence[str],
                            result: subprocess.CompletedProcess) -> None:
    raise GitEnvironmentalError(_git_error(cwd, args, result))

def _git_reports_not_a_repository(result: subprocess.CompletedProcess) -> bool:
    """Recognize Git's documented non-repository diagnostic, not arbitrary 128."""
    detail = (result.stderr or result.stdout or "").strip().lower()
    return (result.returncode == 128
            and detail.startswith("fatal: not a git repository"))


def _require_component(value: str, label: str, error_type: type) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise error_type("{0} must be one portable path/ref component".format(label))
    return value


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _portable_relative(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if (value == "." or value.startswith("/") or "\\" in value or ":" in value or
            "\x00" in value):
        return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _declared_path(root: Path, value: Any, label: str, error_type: type) -> Path:
    if not _portable_relative(value):
        raise error_type("{0} is not a portable relative path".format(label))
    resolved = (root / value).resolve()
    if not _is_under(resolved, root):
        raise error_type("{0} escapes its declared root".format(label))
    return resolved


def _require_repository(path: Path) -> Path:
    """Require an existing worktree root, not merely a directory inside one."""
    if not path.is_dir():
        raise RepositoryPathError("repository path does not exist: {0}".format(path))
    args = ("rev-parse", "--is-inside-work-tree")
    result = _git(path, *args)
    if result.returncode:
        if result.returncode != 1 and not _git_reports_not_a_repository(result):
            _unexpected_git_failure(path, args, result)
        raise RepositoryPathError(
            "repository path is not a Git worktree: {0}".format(path))
    if result.stdout.strip() != "true":
        raise RepositoryPathError("repository path is not a Git worktree: {0}".format(path))
    args = ("rev-parse", "--show-toplevel")
    top_level = _git(path, *args)
    if top_level.returncode:
        _unexpected_git_failure(path, args, top_level)
    if Path(top_level.stdout.strip()).resolve() != path.resolve():
        raise RepositoryPathError("repository path is not the Git worktree root: {0}".format(path))
    return path.resolve()


def repository_binding(path: Path) -> cs.RepositoryPathBinding:
    """Return the canonical root plus immutable Git common-dir filesystem identity."""
    root = _require_repository(Path(path).resolve())
    args = ("rev-parse", "--git-common-dir")
    common = _git(root, *args)
    if common.returncode:
        _unexpected_git_failure(root, args, common)
    raw_common = common.stdout.strip()
    if not raw_common or "\n" in raw_common:
        raise GitEnvironmentalError(
            "Git returned no unambiguous common directory for {0}".format(root))
    common_dir = Path(raw_common)
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    try:
        common_dir = common_dir.resolve()
        details = common_dir.stat()
    except OSError as exc:
        raise GitEnvironmentalError(
            "Git common directory is unavailable for {0}: {1}".format(
                root, exc)) from exc
    if not common_dir.is_dir():
        raise GitEnvironmentalError(
            "Git common directory is not a directory for {0}".format(root))
    return cs.RepositoryPathBinding(
        resolved_path=str(root), git_common_dir=str(common_dir),
        repository_identity="{0:x}:{1:x}".format(details.st_dev, details.st_ino))


def _commit_at(repository_path: Path, revision: str, error_type: type,
               label: str) -> str:
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise error_type("{0} must be an exact 40- or 64-hex commit SHA".format(label))
    args = ("rev-parse", "--verify", "--quiet", revision + "^{commit}")
    result = _git(repository_path, *args)
    if result.returncode:
        if result.returncode != 1:
            _unexpected_git_failure(repository_path, args, result)
        raise error_type(_git_error(repository_path, args, result))
    resolved = result.stdout.strip().lower()
    if resolved != revision.lower():
        raise error_type("{0} does not resolve to its exact declared SHA".format(label))
    return resolved


def _branch_commit(repository_path: Path, branch: str) -> Optional[str]:
    ref = "refs/heads/{0}".format(branch)
    args = ("rev-parse", "--verify", "--quiet", ref + "^{commit}")
    result = _git(repository_path, *args)
    if result.returncode == 1:
        return None
    if result.returncode:
        _unexpected_git_failure(repository_path, args, result)
    resolved = result.stdout.strip().lower()
    if not _SHA.fullmatch(resolved):
        raise GitEnvironmentalError(
            "Git returned a non-commit branch value for {0}".format(ref))
    return resolved


def _candidate_identity(run_id: str, spec: wm.RepositorySpec,
                        state_root: Path) -> Tuple[str, Path]:
    run = _require_component(run_id, "run_id", CandidatePreparationError)
    repository_id = _require_component(spec.repository_id, "repository_id",
                                       CandidatePreparationError)
    branch = "maestro/workspace/{0}/{1}/candidate".format(run, repository_id)
    path = state_root / "candidates" / run / repository_id / "candidate"
    return branch, path

def _safe_candidate_worktree_path(state_root: Path, candidate_path: Path) -> None:
    """Reject links or escapes in every deterministic candidate path component."""
    if state_root.exists() and not state_root.is_dir():
        raise CandidatePreparationError("state_root is not a directory")
    if not _is_under(candidate_path.resolve(), state_root):
        raise CandidatePreparationError("candidate worktree escapes state_root")
    current = state_root
    for component in candidate_path.relative_to(state_root).parts:
        current = current / component
        if current.is_symlink() or current.resolve() != current:
            raise CandidatePreparationError("candidate worktree path contains a symlink")
        if current.exists() and not current.is_dir():
            raise CandidatePreparationError("candidate worktree path component is not a directory")


def resolve_repository_paths(manifest_dir: Path,
                             workspace: wm.WorkspacePlan) -> Dict[str, Path]:
    """Bind every declaration to one distinct, non-overlapping Git worktree.

    Resolution starts at the manifest directory and follows symlinks before
    collision checks, so aliases cannot bypass either duplicate or containment
    checks.  Every declared base is then proved to be an exact commit object.
    """
    manifest_root = Path(manifest_dir).resolve()
    if not manifest_root.is_dir():
        raise RepositoryPathError("manifest directory does not exist: {0}".format(manifest_root))

    resolved: Dict[str, Path] = {}
    for spec in workspace.repositories:
        if spec.repository_id in resolved:
            raise RepositoryPathError(
                "repository identity {0!r} is declared more than once".format(
                    spec.repository_id))
        path = _declared_path(manifest_root, spec.path,
                              "repository path for {0}".format(spec.repository_id),
                              RepositoryPathError)
        for prior_id, prior_path in resolved.items():
            if path == prior_path:
                raise RepositoryPathError(
                    "repositories {0} and {1} resolve to the same path".format(
                        prior_id, spec.repository_id))
            if _is_under(path, prior_path) or _is_under(prior_path, path):
                raise RepositoryPathError(
                    "repositories {0} and {1} overlap as parent and child".format(
                        prior_id, spec.repository_id))
        resolved[spec.repository_id] = path

    for spec in workspace.repositories:
        repository_path = _require_repository(resolved[spec.repository_id])
        _commit_at(repository_path, spec.base_commit, RepositoryBaseError,
                   "base_commit for {0}".format(spec.repository_id))
        resolved[spec.repository_id] = repository_path
    return resolved


def _bound_writer_plan(repository_path: Path,
                       spec: wm.RepositorySpec) -> Tuple[bytes, pm.Plan]:
    """Read one stored plan snapshot and bind every claim to that exact bytestring."""
    if spec.mode is not wm.RepositoryMode.WRITE:
        raise PlanBindingError("only write repositories declare executable plans")
    repository_root = _require_repository(Path(repository_path).resolve())
    if spec.plan_path is None or spec.plan_digest is None:
        raise PlanBindingError("writer declaration lacks plan binding fields")
    plan_path = _declared_path(repository_root, spec.plan_path, "plan_path", PlanBindingError)
    if not plan_path.is_file():
        raise PlanBindingError("declared plan does not exist: {0}".format(plan_path))
    try:
        stored = plan_path.read_bytes()
    except OSError as exc:
        raise PlanBindingError("cannot read declared plan {0}: {1}".format(plan_path, exc))
    actual_digest = plan_digest.digest_of(stored)
    if actual_digest != spec.plan_digest.lower():
        raise PlanBindingError("declared plan digest does not match stored bytes")
    try:
        plan = pm.parse_bytes(stored)
    except pm.PlanParseError as exc:
        raise PlanBindingError("declared plan does not parse: {0}".format(exc))
    if plan.base_commit.lower() != spec.base_commit.lower():
        raise PlanBindingError("declared plan base_commit does not match repository base_commit")
    if plan.repo != spec.path:
        raise PlanBindingError("declared plan repo does not match repository path")
    return stored, plan


def validated_writer_plan_bytes(repository_path: Path,
                                spec: wm.RepositorySpec) -> bytes:
    """Return the exact one-read, digest- and model-bound plan bytes to copy."""
    stored, _ = _bound_writer_plan(repository_path, spec)
    return stored


def validate_writer_plan(repository_path: Path, spec: wm.RepositorySpec) -> pm.Plan:
    """Compatibility wrapper for callers that need the parsed bound plan."""
    _, plan = _bound_writer_plan(repository_path, spec)
    return plan


def _resume_candidate(expected: CandidateRepository,
                      resume: CandidateRepository) -> CandidateRepository:
    if not isinstance(resume, CandidateRepository) or resume != expected:
        raise CandidatePreparationError("resume candidate is not the exact registered identity")
    if not expected.candidate_worktree.is_dir():
        raise CandidatePreparationError("registered candidate worktree is missing")
    top = _require_repository(expected.candidate_worktree)
    if top != expected.candidate_worktree:
        raise CandidatePreparationError("registered candidate worktree root changed")
    symbolic = _git(expected.candidate_worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    if symbolic.returncode or symbolic.stdout.strip() != expected.candidate_branch:
        raise CandidatePreparationError("registered candidate worktree no longer checks out its branch")
    branch_head = _branch_commit(expected.repository_path, expected.candidate_branch)
    head = _git(expected.candidate_worktree, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode or branch_head != head.stdout.strip().lower():
        raise CandidatePreparationError("registered candidate branch no longer names its worktree head")
    return expected


def prepare_candidate(run_id: str, spec: wm.RepositorySpec, repository_path: Path,
                      state_root: Path, *,
                      resume: Optional[CandidateRepository] = None) -> CandidateRepository:
    """Create the one branch/worktree identity, or prove an exact safe resume."""
    if spec.mode is not wm.RepositoryMode.WRITE:
        raise CandidatePreparationError("only writable repositories have candidates")
    repository_root = _require_repository(Path(repository_path).resolve())
    base = _commit_at(repository_root, spec.base_commit, CandidatePreparationError,
                      "base_commit")
    root = Path(state_root).resolve()
    branch, candidate_path = _candidate_identity(run_id, spec, root)
    _safe_candidate_worktree_path(root, candidate_path)
    expected = CandidateRepository(spec.repository_id, repository_root, base, branch,
                                   candidate_path)
    if resume is not None:
        return _resume_candidate(expected, resume)

    # This is deliberately before any candidate filesystem or ref mutation.
    validate_writer_plan(repository_root, spec)
    if spec.target_branch is None:
        raise CandidatePreparationError("writer declaration lacks target_branch")
    target_head = _branch_commit(repository_root, spec.target_branch)
    if target_head is None:
        raise CandidatePreparationError("declared target branch does not exist or is invalid")
    if target_head != base:
        raise CandidatePreparationError("declared target branch no longer equals exact base_commit")
    if _branch_commit(repository_root, branch) is not None:
        raise CandidatePreparationError("deterministic candidate branch already exists")
    if candidate_path.exists() or candidate_path.is_symlink():
        raise CandidatePreparationError("deterministic candidate worktree path already exists")

    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    result = _git(repository_root, "worktree", "add", "-b", branch,
                  str(candidate_path), base)
    if result.returncode:
        raise CandidatePreparationError(_git_error(
            repository_root, ("worktree", "add", "-b", branch, str(candidate_path), base), result))
    return expected


def _accepted_sha(result: Any) -> str:
    if isinstance(result, Mapping):
        outcome = result.get("outcome")
        accepted = result.get("accepted_sha")
    else:
        outcome = getattr(result, "outcome", None)
        accepted = getattr(result, "accepted_sha", None)
    outcome = getattr(outcome, "value", outcome)
    if outcome != "accepted" or not isinstance(accepted, str) or not _SHA.fullmatch(accepted):
        raise AcceptedResultError("participant result is not an accepted exact-SHA result")
    return accepted.lower()


def verify_accepted(candidate: CandidateRepository, participant_result: Any) -> str:
    """Prove that an accepted child result is its candidate's descendant head."""
    if not isinstance(candidate, CandidateRepository):
        raise AcceptedResultError("accepted result lacks a candidate identity")
    accepted = _accepted_sha(participant_result)
    try:
        source = _require_repository(candidate.repository_path)
        worktree_path = _require_repository(candidate.candidate_worktree)
    except RepositoryPathError as exc:
        raise AcceptedResultError(str(exc))
    if source != candidate.repository_path.resolve() or worktree_path != candidate.candidate_worktree.resolve():
        raise AcceptedResultError("candidate identity does not name Git worktree roots")
    symbolic = _git(worktree_path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if symbolic.returncode or symbolic.stdout.strip() != candidate.candidate_branch:
        raise AcceptedResultError("candidate worktree no longer checks out its declared branch")
    branch_head = _branch_commit(source, candidate.candidate_branch)
    head = _git(worktree_path, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode or branch_head is None:
        raise AcceptedResultError("candidate branch or head no longer exists")
    candidate_head = head.stdout.strip().lower()
    if branch_head != candidate_head:
        raise AcceptedResultError("candidate branch no longer names candidate worktree head")
    if accepted != candidate_head:
        raise AcceptedResultError("accepted_sha does not equal candidate head")
    base = _commit_at(source, candidate.base_commit, AcceptedResultError, "candidate base_commit")
    ancestry = _git(source, "merge-base", "--is-ancestor", base, accepted)
    if ancestry.returncode:
        raise AcceptedResultError("accepted_sha does not descend from exact base_commit")
    stable_symbolic = _git(worktree_path, "symbolic-ref", "--quiet", "--short", "HEAD")
    stable_branch = _branch_commit(source, candidate.candidate_branch)
    stable_head = _git(worktree_path, "rev-parse", "--verify", "HEAD^{commit}")
    if (stable_symbolic.returncode or
            stable_symbolic.stdout.strip() != candidate.candidate_branch or
            stable_branch != accepted or stable_head.returncode or
            stable_head.stdout.strip().lower() != accepted):
        raise AcceptedResultError("candidate branch or head moved during acceptance verification")
    return accepted



def _acceptance_root(state_root: Path, run_id: str, error_type: type) -> Tuple[str, Path]:
    """Derive an acceptance root without following an attacker-controlled link."""
    run = _require_component(run_id, "run_id", error_type)
    state = Path(state_root).resolve()
    parent = state / "acceptance"
    root = parent / run
    if parent.is_symlink() or root.is_symlink():
        raise error_type("deterministic acceptance path must not be a symlink")
    if parent.exists() and not parent.is_dir():
        raise error_type("acceptance parent is not a directory")
    if root.exists() and not root.is_dir():
        raise error_type("deterministic acceptance root is not a directory")
    return run, root

def _checkout_read_only(path: Path) -> None:
    """Remove write permission without following checkout-controlled symlinks."""
    paths = []
    for current, directories, files in os.walk(str(path), topdown=False, followlinks=False):
        current_path = Path(current)
        paths.extend(current_path / name for name in files)
        paths.extend(current_path / name for name in directories)
    paths.append(path)
    for item in paths:
        if item.is_symlink():
            continue
        try:
            item.chmod(item.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        except OSError as exc:
            raise AcceptanceError("cannot make acceptance checkout read-only: {0}".format(exc))


def _checkout_writable_for_removal(path: Path) -> None:
    """Restore owner write access only so Git can remove this acceptance tree."""
    for current, directories, files in os.walk(str(path), topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files + directories:
            item = current_path / name
            if not item.is_symlink():
                item.chmod(item.stat().st_mode | stat.S_IWUSR)
    if not path.is_symlink():
        path.chmod(path.stat().st_mode | stat.S_IWUSR)




def _validate_checkout_symlinks(checkout: Path, acceptance_root: Path) -> None:
    """Reject dangling or externally resolving links committed into a checkout."""
    for current, directories, files in os.walk(
            str(checkout), topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files + directories:
            entry = current_path / name
            if not entry.is_symlink():
                continue
            target = entry.resolve()
            if not entry.exists() or not _is_under(target, acceptance_root):
                raise GateConfigurationError(
                    "acceptance checkout contains a dangling or escaping symlink")

def _validate_acceptance_checkouts(acceptance: AcceptanceWorkspace) -> None:
    root = Path(acceptance.root)
    if root.is_symlink() or not root.is_dir():
        raise GateConfigurationError("acceptance root is missing or symlinked")
    resolved_root = root.resolve()
    for repository_id, checkout in acceptance.repository_paths.items():
        _require_component(repository_id, "repository_id", GateConfigurationError)
        declared = Path(checkout)
        resolved = declared.resolve()
        if (not declared.exists() or not resolved.is_dir() or
                not _is_under(resolved, resolved_root)):
            raise GateConfigurationError(
                "acceptance checkout is missing or escapes acceptance root")
        _validate_checkout_symlinks(resolved, resolved_root)
        try:
            _require_repository(resolved)
            _checkout_read_only(resolved)
        except (RepositoryPathError, AcceptanceError) as exc:
            raise GateConfigurationError(
                "acceptance checkout is not a read-only Git worktree: {0}".format(exc))

def _acceptance_cwd(acceptance: AcceptanceWorkspace, value: str) -> Path:
    if value == ".":
        return acceptance.root
    if not _portable_relative(value):
        raise GateConfigurationError("global gate cwd is not acceptance-relative")
    cwd = (acceptance.root / value).resolve()
    if not _is_under(cwd, acceptance.root) or not cwd.is_dir():
        raise GateConfigurationError("global gate cwd is outside or absent from acceptance root")
    return cwd


def assemble_acceptance(run_id: str, workspace: wm.WorkspacePlan,
                        repository_paths: Mapping[str, Path],
                        accepted_shas: Mapping[str, str],
                        state_root: Path) -> AcceptanceWorkspace:
    """Create one detached, read-only tree per declared repository and manifest it."""
    run, root = _acceptance_root(state_root, run_id, AcceptanceError)
    if root.exists():
        raise AcceptanceError("deterministic acceptance root already exists")
    declared_ids = set()
    writable_ids = set()
    for spec in workspace.repositories:
        repository_id = _require_component(spec.repository_id, "repository_id",
                                           AcceptanceError)
        if repository_id in declared_ids:
            raise AcceptanceError("workspace declares a duplicate repository identity")
        declared_ids.add(repository_id)
        if spec.mode is wm.RepositoryMode.WRITE:
            writable_ids.add(repository_id)
    if set(repository_paths) != declared_ids:
        raise AcceptanceError("resolved repository paths do not exactly match declaration")
    if set(accepted_shas) != writable_ids:
        raise AcceptanceError("accepted SHAs do not exactly match writable declarations")
    repositories_root = root / "repositories"
    created = []
    source_paths: Dict[str, Path] = {}
    checkout_paths: Dict[str, Path] = {}
    shas: Dict[str, str] = {}
    try:
        repositories_root.mkdir(parents=True)
        for spec in workspace.repositories:
            if spec.repository_id not in repository_paths:
                raise AcceptanceError("no resolved repository path for {0}".format(spec.repository_id))
            source = _require_repository(Path(repository_paths[spec.repository_id]).resolve())
            base = _commit_at(source, spec.base_commit, AcceptanceError,
                              "base_commit for {0}".format(spec.repository_id))
            if spec.mode is wm.RepositoryMode.WRITE:
                accepted = accepted_shas.get(spec.repository_id)
                if not isinstance(accepted, str) or not _SHA.fullmatch(accepted):
                    raise AcceptanceError("writable repository lacks an accepted exact SHA")
                sha = _commit_at(source, accepted, AcceptanceError,
                                 "accepted_sha for {0}".format(spec.repository_id))
                ancestry = _git(source, "merge-base", "--is-ancestor", base, sha)
                if ancestry.returncode:
                    raise AcceptanceError("accepted_sha does not descend from exact base_commit")
            else:
                if spec.repository_id in accepted_shas:
                    raise AcceptanceError("read-only repository must use its declared base SHA")
                sha = base
            checkout = repositories_root / spec.repository_id
            result = _git(source, "worktree", "add", "--detach", str(checkout), sha)
            if result.returncode:
                raise AcceptanceError(_git_error(
                    source, ("worktree", "add", "--detach", str(checkout), sha), result))
            created.append((source, checkout))
            _checkout_read_only(checkout)
            source_paths[spec.repository_id] = source
            checkout_paths[spec.repository_id] = checkout
            shas[spec.repository_id] = sha

        manifest = {
            "repositories": {
                repository_id: {
                    "path": checkout_paths[repository_id].relative_to(root).as_posix(),
                    "sha": shas[repository_id],
                }
                for repository_id in checkout_paths
            },
            "schema_version": _MANIFEST_SCHEMA,
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8") + b"\n"
        manifest_path = root / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        manifest_path.chmod(manifest_path.stat().st_mode &
                            ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        return AcceptanceWorkspace(
            run_id=run,
            root=root,
            repository_paths=MappingProxyType(dict(checkout_paths)),
            repository_shas=MappingProxyType(dict(shas)),
            manifest_path=manifest_path,
            source_paths=MappingProxyType(dict(source_paths)),
        )
    except Exception as exc:
        cleanup_failures = []
        for source, checkout in reversed(created):
            try:
                _checkout_writable_for_removal(checkout)
                removed = _git(source, "worktree", "remove", "--force", str(checkout))
                if removed.returncode:
                    cleanup_failures.append(_git_error(
                        source, ("worktree", "remove", "--force", str(checkout)), removed))
            except Exception as cleanup_exc:
                cleanup_failures.append(str(cleanup_exc))
        if cleanup_failures:
            raise CleanupError(
                "acceptance cleanup failed; retain root for reclaim: {0}".format(
                    "; ".join(cleanup_failures))) from exc
        if root.exists():
            try:
                shutil.rmtree(str(root))
            except OSError as cleanup_exc:
                raise CleanupError(
                    "acceptance root cleanup failed; retain root for reclaim: {0}".format(
                        cleanup_exc)) from exc
        raise



def _registered_worktrees(source: Path) -> Tuple[Path, ...]:
    result = _git(source, "worktree", "list", "--porcelain")
    if result.returncode:
        raise CleanupError(_git_error(
            source, ("worktree", "list", "--porcelain"), result))
    worktrees = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):])
        if not path.is_absolute():
            path = source / path
        worktrees.append(path.resolve())
    return tuple(worktrees)


def _safe_acceptance_checkout(root: Path, repository_id: str) -> Path:
    checkout = root / "repositories" / repository_id
    resolved = checkout.resolve()
    if (checkout.is_symlink() or resolved != checkout or
            not _is_under(resolved, root)):
        raise CleanupError("acceptance checkout path escapes its deterministic root")
    return checkout


def _remove_stale_worktree_registration(source: Path, checkout: Path) -> bool:
    """Remove exactly this checkout's stale Git registration, if one remains."""
    common = _git(source, "rev-parse", "--git-common-dir")
    if common.returncode:
        raise CleanupError(_git_error(
            source, ("rev-parse", "--git-common-dir"), common))
    common_path = Path(common.stdout.strip())
    if not common_path.is_absolute():
        common_path = source / common_path
    worktrees = common_path.resolve() / "worktrees"
    if not worktrees.is_dir():
        return False
    for administration in worktrees.iterdir():
        gitdir = administration / "gitdir"
        if not administration.is_dir() or not gitdir.is_file():
            continue
        try:
            registered_gitdir = Path(gitdir.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise CleanupError("cannot read Git worktree registration: {0}".format(exc))
        if not registered_gitdir.is_absolute():
            registered_gitdir = administration / registered_gitdir
        if registered_gitdir.resolve().parent != checkout.resolve():
            continue
        try:
            shutil.rmtree(str(administration))
        except OSError as exc:
            raise CleanupError("cannot remove stale acceptance registration: {0}".format(exc))
        return True
    return False


def _remove_acceptance_path(checkout: Path) -> None:
    if not checkout.exists():
        return
    if checkout.is_symlink():
        raise CleanupError("acceptance checkout must not be a symlink")
    try:
        if checkout.is_dir():
            _checkout_writable_for_removal(checkout)
            shutil.rmtree(str(checkout))
        else:
            checkout.unlink()
    except OSError as exc:
        raise CleanupError("cannot remove partial acceptance checkout: {0}".format(exc))


def reclaim_acceptance(run_id: str, workspace: wm.WorkspacePlan,
                      repository_paths: Mapping[str, Path],
                      state_root: Path) -> None:
    """Safely reclaim a crashed deterministic acceptance root before rebuilding.

    Only declared acceptance checkout paths are removed.  A missing linked
    worktree is reconciled by deleting its own Git administration directory,
    never by a broad ``git worktree prune`` that could affect candidates.
    """
    _, root = _acceptance_root(state_root, run_id, CleanupError)
    source_by_id = {}
    checkout_by_id = {}
    for spec in workspace.repositories:
        repository_id = _require_component(spec.repository_id, "repository_id", CleanupError)
        if repository_id in source_by_id:
            raise CleanupError("workspace declares a duplicate repository identity")
        if repository_id not in repository_paths:
            raise CleanupError("missing resolved repository path for acceptance reclaim")
        try:
            source = _require_repository(Path(repository_paths[repository_id]).resolve())
        except RepositoryPathError as exc:
            raise CleanupError(str(exc))
        source_by_id[repository_id] = source
        checkout_by_id[repository_id] = _safe_acceptance_checkout(root, repository_id)
    if set(repository_paths) != set(source_by_id):
        raise CleanupError("resolved repository paths do not exactly match declaration")

    expected = set(checkout_by_id.values())
    for repository_id, source in source_by_id.items():
        checkout = checkout_by_id[repository_id]
        registered = set(_registered_worktrees(source))
        if checkout.resolve() in registered:
            if checkout.exists():
                _checkout_writable_for_removal(checkout)
                removed = _git(source, "worktree", "remove", "--force", str(checkout))
                if removed.returncode:
                    _remove_acceptance_path(checkout)
                    _remove_stale_worktree_registration(source, checkout)
            else:
                _remove_stale_worktree_registration(source, checkout)
        elif checkout.exists():
            _remove_acceptance_path(checkout)
        else:
            _remove_stale_worktree_registration(source, checkout)

        for registered_path in registered:
            if _is_under(registered_path, root) and registered_path not in expected:
                raise CleanupError("unexpected Git worktree registration under acceptance root")

    if not root.exists():
        return
    try:
        shutil.rmtree(str(root))
    except OSError as exc:
        raise CleanupError("cannot remove acceptance root: {0}".format(exc))

def run_global_gates(
        acceptance: AcceptanceWorkspace,
        gates: Sequence[pm.Gate],
        cancel_requested: Callable[[], bool],
        declared_runners: Optional[Mapping[str, str]] = None,
) -> Tuple[wt.GateResult, ...]:
    """Run declared global gates in order with an explicit cancellation source."""
    if not isinstance(acceptance, AcceptanceWorkspace):
        raise GateConfigurationError("global gates require an AcceptanceWorkspace")
    if not callable(cancel_requested):
        raise GateConfigurationError("global gates require a cancellation callback")
    _validate_acceptance_checkouts(acceptance)
    declared_runners = dict(declared_runners or {})
    #: One resolution per `(runner, cwd)`, not one per gate. Every probe costs
    #: a real collection over the whole tree — ten seconds on the repository
    #: this was measured against — and bounded by `PROBE_TIMEOUT_S` at 180s, so
    #: N gates sharing a runner would pay N times for one answer.
    resolved_runners: Dict[Tuple[str, str], rr.ResolvedRunner] = {}
    results = []
    for index, gate in enumerate(gates):
        if cancel_requested():
            raise wt.GateCancelled("global gate execution was cancelled")
        cwd = _acceptance_cwd(acceptance, gate.cwd)
        scratch = acceptance.root / ".maestro-gate-scratch" / str(index)
        # The runner is resolved here rather than inherited from whatever
        # shell started this process. A global gate runs against a checkout of
        # a participating repository, so the resolution is anchored at that
        # checkout and probed there; an unusable runner raises before the gate
        # is executed rather than producing an unparseable report that reads
        # as an environmental fault.
        #
        # The probe runs under the environment the gate will run under, not
        # under this process's. Establishing capability under an environment
        # the gate will not have proves nothing about the gate, and the two
        # differ by the seven cache redirections `launch_env` overlays —
        # `PYTEST_ADDOPTS` among them, which is exactly the kind of variable
        # that decides whether a collection succeeds.
        key = (gate.runner, str(cwd))
        if key not in resolved_runners:
            try:
                resolved_runners[key] = rr.resolve(
                    gate.runner, cwd, ".",
                    declared=declared_runners.get(gate.runner),
                    env=wt.launch_env(scratch))
            except rr.RunnerUnusable as exc:
                raise GateConfigurationError(
                    "{0}:{1}".format(rr.RUNNER_UNUSABLE, exc.detail)) from exc
        resolved = resolved_runners[key]
        result = wt.run_integration_gate(
            cwd, resolved, tuple(gate.argv), scratch,
            cancel_requested, label="global-gate-{0}".format(index))
        if not result.green or result.counts.get("passed", 0) < gate.min_cases:
            raise GateFailure(index, result, results)
        results.append(result)
    return tuple(results)


def cleanup_acceptance(acceptance: AcceptanceWorkspace) -> None:
    """Remove only this deterministic acceptance object's detached worktrees."""
    if not isinstance(acceptance, AcceptanceWorkspace):
        raise CleanupError("cleanup requires an AcceptanceWorkspace")
    declared_root = Path(acceptance.root)
    if (not declared_root.is_absolute() or declared_root.name != acceptance.run_id or
            declared_root.parent.name != "acceptance"):
        raise CleanupError("acceptance object does not name its deterministic root")
    _, root = _acceptance_root(declared_root.parent.parent, acceptance.run_id, CleanupError)
    if root != declared_root:
        raise CleanupError("acceptance object root does not match its deterministic identity")
    if not root.exists():
        return
    if set(acceptance.repository_paths) != set(acceptance.source_paths):
        raise CleanupError("acceptance object has mismatched checkout identities")
    for repository_id, checkout in reversed(tuple(acceptance.repository_paths.items())):
        _require_component(repository_id, "repository_id", CleanupError)
        checkout_path = _safe_acceptance_checkout(root, repository_id)
        if Path(checkout) != checkout_path:
            raise CleanupError("acceptance checkout does not match its deterministic path")
        source = acceptance.source_paths.get(repository_id)
        if source is None:
            raise CleanupError("acceptance checkout has no source repository")
        try:
            source_path = _require_repository(Path(source).resolve())
        except RepositoryPathError as exc:
            raise CleanupError(str(exc))
        if checkout_path.exists():
            _checkout_writable_for_removal(checkout_path)
            result = _git(source_path, "worktree", "remove", "--force", str(checkout_path))
            if result.returncode:
                raise CleanupError(_git_error(
                    source_path,
                    ("worktree", "remove", "--force", str(checkout_path)), result))
        else:
            _remove_stale_worktree_registration(source_path, checkout_path)
    try:
        shutil.rmtree(str(root))
    except OSError as exc:
        raise CleanupError("cannot remove acceptance root: {0}".format(exc))
