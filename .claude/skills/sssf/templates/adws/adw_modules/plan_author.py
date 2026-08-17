"""Author a canonical `maestro-plan.v1` file from a draft mapping.

This is the only production writer of plan bytes. It fills git-observed
hashes, then writes `canonicalize(plan)` and never rewrites a stored file
on the runtime path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from . import plan_canonical as pc
from . import plan_model as pm
from . import plan_validate as pv


class AuthoringError(ValueError):
    """A draft cannot be authored into canonical plan bytes."""


DRAFT_NAMES = ("draft.json", "draft.yaml", "draft.yml")


def find_draft(plan_dir: Path) -> Path:
    """The conventional draft beside the eventual `maestro-plan.v1`."""
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


def resolve_base_commit(repo: Path, declared: Optional[str]) -> str:
    """Use the draft commit when present; otherwise the repository HEAD."""
    if declared:
        if not pv.commit_exists(repo, declared):
            raise AuthoringError("BASE_COMMIT_MISSING:{}".format(declared))
        return declared
    code, out = pv._git(repo, "rev-parse", "HEAD")
    commit = out.decode("ascii", "replace").strip()
    if code != 0 or not commit:
        raise AuthoringError("BASE_COMMIT_MISSING:HEAD")
    return commit


def fill_git_facts(draft: Mapping[str, Any], repo: Path) -> dict:
    """Copy the draft and fill observed / prompt / produced-base hashes."""
    data = json.loads(json.dumps(draft))
    if data.get("schema_version") is None:
        data["schema_version"] = "maestro-plan.v1"
    repo_name = data.get("repo")
    if not repo_name:
        data["repo"] = Path(repo).name
    data["base_commit"] = resolve_base_commit(repo, data.get("base_commit"))
    commit = data["base_commit"]
    evidence = data.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "observed":
                path = item.get("path")
                if not isinstance(path, str) or not path:
                    continue
                blob = pv.blob_at(repo, commit, path)
                if blob is None:
                    raise AuthoringError(
                        "OBSERVED_PATH_ABSENT:{}@{}".format(path, commit))
                item["sha256"] = hashlib.sha256(blob).hexdigest()
            elif item.get("kind") == "produced":
                path = item.get("path")
                if not isinstance(path, str) or not path:
                    continue
                blob = pv.blob_at(repo, commit, path)
                if blob is not None and not item.get("base_sha256"):
                    item["base_sha256"] = hashlib.sha256(blob).hexdigest()
    nodes = data.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            assets = node.get("prompt_assets")
            if not isinstance(assets, list):
                continue
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                path = asset.get("path")
                if not isinstance(path, str) or not path:
                    continue
                if asset.get("sha256"):
                    continue
                file_path = Path(repo) / path
                if not file_path.is_file():
                    raise AuthoringError("PROMPT_ASSET_MISSING:{}".format(path))
                asset["sha256"] = hashlib.sha256(
                    file_path.read_bytes()).hexdigest()
    return data


def author_plan(draft: Mapping[str, Any], repo: Path) -> bytes:
    """Return canonical `maestro-plan.v1` bytes for a filled draft."""
    filled = fill_git_facts(draft, repo)
    try:
        plan = pm.parse_mapping(filled)
    except pm.PlanParseError as exc:
        raise AuthoringError("PLAN_DRAFT_INVALID:{}".format(exc)) from exc
    return pc.canonicalize(plan)


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


def author_from_draft(draft_path: Path, destination: Path, repo: Path) -> bytes:
    """Load a draft file, canonicalize it, and write `destination`."""
    stored = author_plan(load_draft(draft_path), repo)
    write_canonical_plan(destination, stored)
    return stored
