"""Compiler-only plan parse/refusal types. Shared DTOs live in scheduler_types."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Any, Mapping, Optional, Tuple

SCHEMA_VERSION = "maestro-plan.artifact-factory.v1"
NO_PLAN_ARTIFACT_REF = "NO_PLAN_ARTIFACT_REF"

PLAN_KEYS = frozenset({"schema_version", "lanes"})
LANE_KEYS = frozenset({"id", "needs", "outputs", "spec", "acceptance"})


class PlanParseError(ValueError):
    """Stored bytes are not a UTF-8 JSON object."""


@dataclass(frozen=True)
class PlanRefusal:
    """One objective compiler refusal. Not a workflow stage."""

    code: str
    pointer: str
    message: str


class PlanCompileError(ValueError):
    """The authored plan failed an objective check."""

    def __init__(self, refusals: Tuple[PlanRefusal, ...]) -> None:
        if not refusals:
            raise ValueError("PlanCompileError requires at least one refusal")
        self.refusals = refusals
        super().__init__(
            "; ".join(
                "{0} {1}: {2}".format(item.code, item.pointer, item.message)
                for item in refusals
            )
        )


def parse_stored_mapping(stored: bytes) -> Mapping[str, Any]:
    if not isinstance(stored, (bytes, bytearray)):
        raise PlanParseError("plan bytes are required")
    try:
        data = json.loads(bytes(stored).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanParseError("not UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise PlanParseError("plan root must be a JSON object")
    return data


def normalize_declared_output(raw: Any) -> Optional[str]:
    """Exact repository-relative POSIX file path, or None if inadmissible.

    Directories, globs, absolute paths, empty/`.`/`..` components, doubled
    separators, and non-operator spellings are refused rather than rewritten.
    Comparison is byte-exact on the returned string. No filesystem is consulted.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None
    if "\\" in raw or "//" in raw:
        return None
    if raw.endswith("/"):
        return None
    if any(char in raw for char in "*?[]"):
        return None
    candidate = PurePosixPath(raw)
    native = PurePath(raw)
    if candidate.is_absolute() or native.is_absolute() or candidate.anchor:
        return None
    parts = candidate.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    if candidate.as_posix() != raw:
        return None
    return raw


def outputs_conflict(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")
