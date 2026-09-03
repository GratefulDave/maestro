"""Author a canonical SCHEMA_VERSION file from a draft mapping.

This is the only production writer of plan bytes. It runs the objective
compiler, then writes canonicalize(plan) and never rewrites a stored file
on the runtime path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import plan_canonical as pc
from . import plan_compiler
from .plan_model import PlanCompileError, SCHEMA_VERSION


class AuthoringError(ValueError):
    """A draft cannot be authored into canonical plan bytes."""


DRAFT_NAMES = ("draft.json", "draft.yaml", "draft.yml")


def find_draft(plan_dir: Path) -> Path:
    """The conventional draft beside the eventual SCHEMA_VERSION plan."""
    for name in DRAFT_NAMES:
        candidate = Path(plan_dir) / name
        if candidate.is_file():
            return candidate
    raise AuthoringError("PLAN_DRAFT_MISSING:{}".format(plan_dir))


def load_draft(path: Path) -> dict:
    """Parse a JSON or YAML draft object."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise AuthoringError("PLAN_DRAFT_UNREADABLE:{}".format(path)) from exc
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(text)
        else:
            payload = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise AuthoringError("PLAN_DRAFT_UNPARSEABLE:{}".format(path)) from exc
    if not isinstance(payload, dict):
        raise AuthoringError("PLAN_DRAFT_NOT_OBJECT:{}".format(path))
    return payload


def author_plan(draft: Mapping[str, Any]) -> bytes:
    """Canonical bytes for a draft the objective compiler admits."""
    stored = pc.canonicalize(draft)
    try:
        plan_compiler.compile_plan(stored)
    except PlanCompileError as exc:
        raise AuthoringError("PLAN_DRAFT_INVALID:{}".format(exc)) from exc
    return stored


def write_canonical_plan(destination: Path, stored: bytes) -> Path:
    """Create-once write of already-canonical plan bytes."""
    path = Path(destination)
    if path.exists():
        raise AuthoringError("PLAN_EXISTS:{}".format(path))
    if not pc.is_canonical(stored):
        raise AuthoringError("PLAN_NOT_CANONICAL")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(stored)
    tmp.replace(path)
    return path


def author_from_draft(draft_path: Path, destination: Path) -> bytes:
    """Load a draft file, canonicalize it, and write `destination`."""
    stored = author_plan(load_draft(draft_path))
    write_canonical_plan(destination, stored)
    return stored
