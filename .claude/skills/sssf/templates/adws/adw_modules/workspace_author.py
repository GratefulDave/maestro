"""Assemble maestro-workspace.v1 from finalized child plans only."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Tuple

from . import finalization
from . import plan_digest
from . import workspace_canonical as wc
from . import workspace_model as wm


class WorkspaceAuthoringError(ValueError):
    """A workspace draft cannot be authored from child receipts."""


def _load_json(path: Path, code: str) -> dict:
    try:
        payload = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceAuthoringError("{}:{}".format(code, path)) from exc
    if not isinstance(payload, dict):
        raise WorkspaceAuthoringError("{}:not-object".format(code))
    return payload


def _child_digest_and_base(plan_path: Path) -> Tuple[str, str]:
    stored = Path(plan_path).read_bytes()
    digest = plan_digest.digest_of(stored)
    mapping = json.loads(stored.decode("utf-8"))
    base = mapping.get("base_commit")
    if not isinstance(base, str) or not base:
        raise WorkspaceAuthoringError("CHILD_BASE_MISSING:{}".format(plan_path))
    return digest, base


def _require_pass_receipt(receipt_path: Path, digest: str) -> None:
    try:
        receipt = finalization.Receipt.from_bytes(Path(receipt_path).read_bytes())
    except (OSError, finalization.ReceiptInvalid) as exc:
        raise WorkspaceAuthoringError(
            "CHILD_RECEIPT_INVALID:{}".format(receipt_path)) from exc
    if receipt.verdict is not finalization.Verdict.PASS:
        raise WorkspaceAuthoringError("CHILD_RECEIPT_NOT_PASS:{}".format(digest))
    if receipt.plan_digest != digest:
        raise WorkspaceAuthoringError("CHILD_RECEIPT_DIGEST_MISMATCH")


def author_workspace(draft: Mapping[str, Any], root: Path) -> bytes:
    """Fill write-child digests from stored plans and require PASS receipts."""
    if draft.get("publication_mode", "none") != "none":
        raise WorkspaceAuthoringError("PUBLICATION_NOT_NONE")
    repositories = draft.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise WorkspaceAuthoringError("REPOSITORIES_MISSING")
    filled = []
    for item in repositories:
        if not isinstance(item, dict):
            raise WorkspaceAuthoringError("REPOSITORY_NOT_OBJECT")
        row = dict(item)
        mode = row.get("mode")
        if mode == "write":
            plan_path = row.get("plan_path")
            receipt_path = row.pop("receipt_path", None)
            if not isinstance(plan_path, str) or not isinstance(receipt_path, str):
                raise WorkspaceAuthoringError(
                    "WRITE_CHILD_PATHS:{}".format(row.get("repository_id")))
            digest, base = _child_digest_and_base(Path(root) / plan_path)
            _require_pass_receipt(Path(root) / receipt_path, digest)
            row["plan_digest"] = digest
            row["base_commit"] = base
        elif mode == "read_only":
            if any(key in row for key in ("plan_path", "plan_digest", "receipt_path",
                                          "target_branch", "run_argv", "needs")):
                raise WorkspaceAuthoringError(
                    "READ_ONLY_FORBIDDEN:{}".format(row.get("repository_id")))
        else:
            raise WorkspaceAuthoringError("REPOSITORY_MODE:{}".format(mode))
        filled.append(row)
    payload = {
        "schema_version": "maestro-workspace.v1",
        "workspace_id": draft["workspace_id"],
        "publication_mode": "none",
        "repositories": filled,
        "integration_gates": list(draft.get("integration_gates") or ()),
    }
    try:
        workspace = wm.parse_mapping(payload)
    except wm.WorkspaceParseError as exc:
        raise WorkspaceAuthoringError("WORKSPACE_INVALID:{}".format(exc)) from exc
    return wc.canonicalize_workspace(workspace)


def author_from_draft(draft_path: Path, destination: Path, root: Path) -> bytes:
    draft = _load_json(draft_path, "WORKSPACE_DRAFT_UNREADABLE")
    stored = author_workspace(draft, root)
    path = Path(destination)
    if path.exists():
        raise WorkspaceAuthoringError("WORKSPACE_EXISTS:{}".format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(stored)
    tmp.replace(path)
    return stored
