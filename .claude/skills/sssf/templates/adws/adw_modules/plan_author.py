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


def _blob_or_refuse(repo: Path, commit: str, path: str) -> Optional[bytes]:
    """`pv.blob_at`, with its non-file answer turned into an authoring refusal.

    A path that exists at the base commit and is not a blob — a directory,
    most often — is a defect in the draft, so it refuses here rather than
    reaching `pm.parse_mapping` as a missing hash. `GitReadFailed` is
    deliberately not caught: git failing is the machine, not the draft, and
    turning it into an `AuthoringError` would be the same conflation §7.5
    forbids one layer up.
    """
    try:
        return pv.blob_at(repo, commit, path)
    except pv.GitPathNotAFile as exc:
        raise AuthoringError("OBSERVED_PATH_NOT_A_FILE:{}".format(exc)) from exc


def fill_git_facts(draft: Mapping[str, Any], repo: Path) -> dict:
    """Copy the draft and fill observed / prompt / produced-base hashes."""
    data = json.loads(json.dumps(draft))
    if data.get("schema_version") is None:
        data["schema_version"] = "maestro-plan.v1"
    # Keyed on absence, not on falsiness: `"repo": ""` is a malformed draft,
    # not an omitted field, and silently substituting the directory name for
    # it is the same substitution this function used to perform on hashes.
    # An empty string reaches `pm.parse_mapping` and is refused there.
    if data.get("repo") is None:
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
                blob = _blob_or_refuse(repo, commit, path)
                if blob is None:
                    raise AuthoringError(
                        "OBSERVED_PATH_ABSENT:{}@{}".format(path, commit))
                # Fill what the author did not declare; never replace what
                # they did. The unguarded assignment this replaces is what
                # made every `source_artifacts` pin theatre: the author
                # declares a hash, this line overwrote it with the hash of the
                # object it was supposed to be checked against, and
                # `plan_validate._evidence_typed_against_git` then compared a
                # value with the thing it had just been computed from. That
                # obligation exists to convict a fabricated citation — the
                # `Observed` docstring says so — and it could not go red for
                # as long as this line ran. A check that cannot fail is §7.4's
                # green pre-gate wearing a validator's clothes.
                #
                # The sibling `produced` branch below has always been guarded
                # this way; the two now agree, and the guarded one was right.
                actual = hashlib.sha256(blob).hexdigest()
                declared = item.get("sha256")
                if declared and declared != actual:
                    raise AuthoringError(
                        "OBSERVED_DIGEST_MISMATCH:{}@{}:declared {}, object "
                        "hashes to {}".format(path, commit, declared, actual))
                item["sha256"] = actual
            elif item.get("kind") == "produced":
                path = item.get("path")
                if not isinstance(path, str) or not path:
                    continue
                blob = _blob_or_refuse(repo, commit, path)
                if blob is None:
                    continue
                # Symmetric with `observed` above, and for the same reason.
                # A declared `base_sha256` that disagrees with the object was
                # previously left for `plan validate` to convict — but
                # `write_canonical_plan` is create-once (`PLAN_EXISTS`), so a
                # plan authored invalid has to be deleted by hand before the
                # corrected draft can be authored at all. Catching it before
                # the file exists is strictly better, and the two branches
                # agreeing removes the question of which one to trust.
                actual = hashlib.sha256(blob).hexdigest()
                declared = item.get("base_sha256")
                if declared and declared != actual:
                    raise AuthoringError(
                        "PRODUCED_BASE_DIGEST_MISMATCH:{}@{}:declared {}, "
                        "object hashes to {}".format(
                            path, commit, declared, actual))
                item["base_sha256"] = actual
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
