"""Canonical workspace bytes for authoring and validation only.

Runtime identity is deliberately elsewhere: this module parses and serializes
``WorkspacePlan`` values, while ``workspace_digest`` only hashes stored bytes.
"""

from __future__ import annotations

import json

from .workspace_model import WorkspaceParseError, WorkspacePlan, parse_bytes


def canonicalize_workspace(workspace: WorkspacePlan) -> bytes:
    """Return sorted, compact UTF-8 JSON with exactly one trailing newline."""
    payload = workspace.model_dump(mode="json")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return text.encode("utf-8") + b"\n"


def is_canonical(stored: bytes) -> bool:
    """Whether stored workspace bytes are valid and already canonical."""
    try:
        workspace = parse_bytes(stored)
    except WorkspaceParseError:
        return False
    return canonicalize_workspace(workspace) == stored
