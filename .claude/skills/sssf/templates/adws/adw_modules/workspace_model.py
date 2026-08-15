"""The frozen ``maestro-workspace.v1`` shared coordinator schema.

A workspace declares participant repositories and their cross-repository
ordering.  Parsing is closed: unknown fields, malformed repository metadata,
and an impossible dependency graph are all typed refusals with locations in
the authored JSON document.
"""

from __future__ import annotations

import json
import posixpath
from enum import Enum
from typing import Annotated, Any, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator, model_serializer, model_validator)

from .plan_model import Gate

SCHEMA_V1 = "maestro-workspace.v1"

_STRICT = ConfigDict(extra="forbid", frozen=True)
_GIT_OBJECT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REPOSITORY_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
_WRITE_FIELDS = ("plan_path", "plan_digest", "target_branch", "run_argv")
_READ_ONLY_FORBIDDEN_FIELDS = _WRITE_FIELDS + ("needs",)


class RepositoryMode(str, Enum):
    """Whether Maestro may execute and publish work in a repository."""

    WRITE = "write"
    READ_ONLY = "read_only"


class PublicationMode(str, Enum):
    """The permitted publication mechanism for successful workspace work."""

    NONE = "none"
    LOCAL_REFS = "local_refs"
    PULL_REQUESTS = "pull_requests"


class RepositoryState(str, Enum):
    """The authoritative execution state of one participant repository."""

    PENDING = "pending"
    RUNNING = "running"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkspaceOutcome(str, Enum):
    """The terminal coordinator outcome for a workspace."""

    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    PARTIALLY_PUBLISHED = "partially_published"
    PUBLISHED = "published"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


class PublicationState(str, Enum):
    """The state of a repository's publication transaction."""

    PENDING = "pending"
    PREPARED = "prepared"
    PUBLISHED = "published"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class WorkspaceParseError(ValueError):
    """A workspace document that fails to parse closed.

    ``pointers`` contains ``(json_pointer, message)`` pairs so callers can
    present an authored-document error rather than a Pydantic traceback.
    """

    def __init__(self, message: str,
                 pointers: Sequence[Tuple[str, str]] = ()) -> None:
        super().__init__(message)
        self.pointers: Tuple[Tuple[str, str], ...] = tuple(pointers)


def _portable_relative_path(value: str) -> str:
    """Refuse paths whose interpretation varies by host operating system."""
    if (value == "." or value.startswith("/") or "\\" in value or
            ":" in value or "\x00" in value or
            any(part in (".", "..") for part in value.split("/")) or
            posixpath.normpath(value) != value):
        raise ValueError(
            "must be a normalized portable relative POSIX path")
    return value

def validate_git_branch_ref_fragment(value: str) -> str:
    """Refuse branch names that Git cannot safely interpret as ``refs/heads``."""
    if not isinstance(value, str) or not value:
        raise ValueError("must be a nonempty Git branch ref fragment")
    if (value.startswith(("-", "/", ".")) or value.endswith(("/", "."))
            or value == "@" or ".." in value or "@{" in value):
        raise ValueError("must be a valid Git branch ref fragment")
    if (any(character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value)
            or any(character in "~^:?*[\\" for character in value)):
        raise ValueError("must be a valid Git branch ref fragment")
    components = value.split("/")
    if any(not component or component.startswith(".")
           or component.endswith(".lock") for component in components):
        raise ValueError("must be a valid Git branch ref fragment")
    return value


class RepositorySpec(BaseModel):
    """One participant repository and, for writers, its executable plan."""

    model_config = _STRICT

    repository_id: str = Field(pattern=_REPOSITORY_ID_PATTERN)
    mode: RepositoryMode
    path: str = Field(min_length=1)
    base_commit: str = Field(pattern=_GIT_OBJECT_PATTERN)
    remote: Optional[str] = Field(default=None, min_length=1)
    needs: Tuple[Annotated[str, Field(min_length=1)], ...] = ()
    plan_path: Optional[str] = None
    plan_digest: Optional[str] = Field(default=None, pattern=_SHA256_PATTERN)
    target_branch: Optional[str] = Field(default=None, min_length=1)
    run_argv: Tuple[Annotated[str, Field(min_length=1)], ...] = ()

    @field_validator("path")
    @classmethod
    def _path_is_portable(cls, value: str) -> str:
        return _portable_relative_path(value)

    @field_validator("remote")
    @classmethod
    def _remote_is_not_an_option(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value.startswith("-"):
            raise ValueError("remote must not begin with '-'")
        return value

    @field_validator("plan_path")
    @classmethod
    def _plan_path_is_portable(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _portable_relative_path(value)

    @field_validator("target_branch")
    @classmethod
    def _target_branch_is_git_ref_fragment(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_git_branch_ref_fragment(value)

    @model_validator(mode="after")
    def _enforce_mode_fields(self) -> "RepositorySpec":
        if self.mode is RepositoryMode.WRITE:
            missing = []
            if self.plan_path is None:
                missing.append("plan_path")
            if self.plan_digest is None:
                missing.append("plan_digest")
            if self.target_branch is None:
                missing.append("target_branch")
            if not self.run_argv:
                missing.append("run_argv")
            if missing:
                raise ValueError(
                    "write repositories require {0}".format(
                        ", ".join(missing)))
            return self

        present = [
            field for field in _READ_ONLY_FORBIDDEN_FIELDS
            if field in self.model_fields_set
        ]
        if present:
            raise ValueError(
                "read-only repositories forbid {0}".format(
                    ", ".join(present)))
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> Any:
        """Do not materialize absent write-only fields for a read-only spec.

        This keeps model persistence and canonical bytes in the same authored
        shape while still giving callers one typed ``RepositorySpec``.
        """
        serialized = handler(self)
        if self.mode is RepositoryMode.READ_ONLY:
            for field in _READ_ONLY_FORBIDDEN_FIELDS:
                serialized.pop(field, None)
        return serialized


class WorkspacePlan(BaseModel):
    """``maestro-workspace.v1`` and its dependency invariants."""

    model_config = _STRICT

    schema_version: Literal["maestro-workspace.v1"]
    workspace_id: str = Field(min_length=1)
    repositories: Tuple[RepositorySpec, ...] = Field(min_length=1)
    publication_mode: PublicationMode = PublicationMode.NONE
    integration_gates: Tuple[Gate, ...] = ()

    @model_validator(mode="after")
    def _validate_repositories(self) -> "WorkspacePlan":
        by_id = {}
        paths = set()
        for repository in self.repositories:
            if repository.repository_id in by_id:
                raise ValueError(
                    "repository_id {0!r} is declared more than once".format(
                        repository.repository_id))
            by_id[repository.repository_id] = repository

            portable_key = repository.path.casefold()
            if portable_key in paths:
                raise ValueError(
                    "repository path {0!r} is declared more than once".format(
                        repository.path))
            paths.add(portable_key)

        for repository in self.repositories:
            for needed_id in repository.needs:
                if needed_id == repository.repository_id:
                    raise ValueError(
                        "repository {0!r} cannot need itself".format(
                            repository.repository_id))
                if needed_id not in by_id:
                    raise ValueError(
                        "repository {0!r} needs undeclared repository {1!r}"
                        .format(repository.repository_id, needed_id))

        completed = set()
        visiting = []

        def visit(repository_id: str) -> None:
            if repository_id in completed:
                return
            if repository_id in visiting:
                cycle = visiting[visiting.index(repository_id):] + [repository_id]
                raise ValueError(
                    "repository dependencies contain a cycle: {0}".format(
                        " -> ".join(cycle)))
            visiting.append(repository_id)
            for needed_id in by_id[repository_id].needs:
                visit(needed_id)
            visiting.pop()
            completed.add(repository_id)

        for repository in self.repositories:
            visit(repository.repository_id)

        if self.publication_mode is PublicationMode.PULL_REQUESTS:
            for repository in self.repositories:
                if (repository.mode is RepositoryMode.WRITE and
                        repository.remote is None):
                    raise ValueError(
                        "pull_requests publication requires a remote for write "
                        "repository {0!r}".format(repository.repository_id))
        return self


def _pointer(loc: Sequence[Any]) -> str:
    def escape(part: Any) -> str:
        return str(part).replace("~", "~0").replace("/", "~1")

    return "/" + "/".join(escape(part) for part in loc)
def _contains_surrogate(value: Any) -> bool:
    """Whether decoded JSON contains a code point UTF-8 cannot encode."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF
                   for character in current):
                return True
        elif isinstance(current, Mapping):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return False






def parse_mapping(data: Mapping[str, Any]) -> WorkspacePlan:
    """Parse one frozen workspace version with typed, located refusals."""
    if not isinstance(data, Mapping):
        raise WorkspaceParseError(
            "a workspace file is a JSON object, got {0}".format(
                type(data).__name__),
            (("", "not an object"),))

    version = data.get("schema_version")
    if version != SCHEMA_V1:
        message = ("missing" if version is None else
                   "unregistered schema version {0!r}".format(version))
        raise WorkspaceParseError(
            "the workspace declares {0}; no default or upgrade exists".format(
                message),
            (("/schema_version", message),))


    try:
        return WorkspacePlan.model_validate(dict(data))
    except ValidationError as exc:
        pointers = tuple((_pointer(error["loc"]), error["msg"])
                         for error in exc.errors())
        raise WorkspaceParseError(
            "the workspace does not parse closed under WorkspacePlan: {0}".format(
                "; ".join("{0} {1}".format(pointer, message)
                          for pointer, message in pointers)),
            pointers)


def parse_bytes(stored: bytes) -> WorkspacePlan:
    """Parse stored UTF-8 JSON without changing its byte representation."""
    if not isinstance(stored, (bytes, bytearray)):
        raise WorkspaceParseError(
            "the workspace file is not stored bytes",
            (("", "not stored bytes"),))
    try:
        data = json.loads(bytes(stored).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise WorkspaceParseError(
            "the workspace file is not UTF-8 JSON: {0}".format(exc),
            (("", "not UTF-8 JSON"),))
    if _contains_surrogate(data):
        raise WorkspaceParseError(
            "the workspace file contains a surrogate code point",
            (("", "contains a surrogate code point"),))
    try:
        return parse_mapping(data)
    except RecursionError as exc:
        raise WorkspaceParseError(
            "the workspace file is structurally too deep: {0}".format(exc),
            (("", "structurally too deep"),))
