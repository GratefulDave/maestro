"""Vault-boundary helpers for private-review payloads.

Does not write lane_state. Canonical identity lives in scheduler_types:
LaneArtifact, ArtifactKind, ReviewerVerdict, canonical_bytes, digest_bytes.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import scheduler_types as st

_REDACTED = "[redacted]"
_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_QUOTED_LITERAL = re.compile(r"""(['"])(.*?)\1""")
_SECRET_TOKEN_MIN = 8


class PrivateReviewError(ValueError):
    """A private-review payload could not be constructed."""


class PrivateLeakError(PrivateReviewError):
    """Public bytes contained private test or vault material."""


class IsolationError(PrivateReviewError):
    """Private objects were reachable from a run or builder repository."""


@dataclass(frozen=True)
class VaultLaneRequest:
    """Slice identity needed to name vault refs and fill LaneArtifact."""

    run_id: str
    lane_id: str
    plan_revision: int
    spec_digest: str
    lane_projection_digest: str
    input_digest: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.run_id):
            raise PrivateReviewError("run_id is invalid")
        if not _ID.fullmatch(self.lane_id):
            raise PrivateReviewError("lane_id is invalid")
        if not isinstance(self.plan_revision, int) or self.plan_revision < 1:
            raise PrivateReviewError("plan_revision must be a positive int")
        st.require_hex_digest(self.spec_digest, name="spec_digest")
        st.require_hex_digest(
            self.lane_projection_digest, name="lane_projection_digest"
        )
        st.require_hex_digest(self.input_digest, name="input_digest")


def make_lane_artifact(
    *,
    kind: st.ArtifactKind,
    request: VaultLaneRequest,
    payload: Mapping[str, Any],
    artifact_ref: str,
    verdict: st.ReviewerVerdict | None = None,
) -> st.LaneArtifact:
    ready = st.json_ready(payload)
    return st.LaneArtifact(
        kind=kind,
        plan_revision=request.plan_revision,
        spec_digest=request.spec_digest,
        lane_projection_digest=request.lane_projection_digest,
        input_digest=request.input_digest,
        output_digest=st.digest_canonical(ready),
        artifact_ref=artifact_ref,
        payload=ready,
        verdict=verdict,
    )


def public_contract(
    *,
    acceptance_criteria: Sequence[str],
    declared_outputs: Sequence[str],
) -> dict[str, Any]:
    criteria = tuple(
        _nonempty(item, "acceptance criterion") for item in acceptance_criteria
    )
    outputs = tuple(normalize_repo_path(item) for item in declared_outputs)
    if not criteria:
        raise PrivateReviewError("public_contract requires acceptance_criteria")
    if not outputs:
        raise PrivateReviewError("public_contract requires declared_outputs")
    return {
        "acceptance_criteria": list(criteria),
        "declared_outputs": list(outputs),
    }


def normalize_repo_path(path: str) -> str:
    raw = path.replace("\\", "/")
    if not raw or raw.startswith("/") or raw.endswith("/"):
        raise PrivateReviewError("path is not a repository-relative file")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PrivateReviewError("path is not a normalized POSIX file")
    return posixpath.normpath(raw)


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PrivateReviewError("{0} must be a nonempty string".format(label))
    return value.strip()


def as_str_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PrivateReviewError("{0} must be a list".format(label))
    return tuple(str(item) for item in value)


def collect_private_tokens(
    *,
    files: Mapping[str, str] | None = None,
    extra: Iterable[str] = (),
    vault_path: Path | None = None,
    vault_refs: Iterable[str] = (),
    blob_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    tokens: list[str] = []
    if vault_path is not None:
        tokens.append(str(Path(vault_path).resolve()))
    tokens.extend(ref for ref in vault_refs if ref)
    tokens.extend(blob for blob in blob_ids if blob)
    if files:
        for path, body in files.items():
            tokens.append(path)
            tokens.append(posixpath.basename(path))
            tokens.append(body)
            for match in _QUOTED_LITERAL.finditer(body):
                literal = match.group(2)
                if len(literal) >= _SECRET_TOKEN_MIN:
                    tokens.append(literal)
            for line in body.splitlines():
                stripped = line.strip()
                if len(stripped) >= _SECRET_TOKEN_MIN:
                    tokens.append(stripped)
    tokens.extend(item for item in extra if item)
    unique = []
    seen = set()
    for token in sorted(tokens, key=len, reverse=True):
        if token and token not in seen:
            seen.add(token)
            unique.append(token)
    return tuple(unique)


def redact_text(value: str, tokens: Sequence[str]) -> str:
    text = value
    for token in tokens:
        if token and token in text:
            text = text.replace(token, _REDACTED)
    return text


def redact_findings(
    findings: Sequence[Mapping[str, str]],
    tokens: Sequence[str],
) -> tuple[dict[str, str], ...]:
    out = []
    for item in findings:
        row = {
            key: redact_text(str(item[key]), tokens) for key in st.REVISE_FINDING_KEYS
        }
        out.append(row)
    return tuple(out)


def actionable_findings(
    verdict: st.ReviewerVerdict,
    findings: Sequence[Mapping[str, str]],
    tokens: Sequence[str] = (),
) -> tuple[Mapping[str, Any], ...]:
    if verdict is st.ReviewerVerdict.PASS:
        if findings:
            raise PrivateReviewError("PASS findings must be empty")
        return ()
    if verdict is not st.ReviewerVerdict.REVISE:
        raise PrivateReviewError("verdict must be PASS or REVISE")
    checked = st.require_revise_findings(findings)
    return st.require_revise_findings(redact_findings(checked, tokens))


def refuse_private_leak(
    obj: object, tokens: Sequence[str], *, allow: Iterable[str] = ()
) -> None:
    allowed = frozenset(allow)
    if isinstance(obj, (bytes, bytearray)):
        blob = bytes(obj).decode("utf-8")
    elif isinstance(obj, str):
        blob = obj
    else:
        blob = st.canonical_bytes(obj).decode("utf-8")
    for token in tokens:
        if not token or token in allowed:
            continue
        if token in blob:
            raise PrivateLeakError("public payload leaked private token")


def write_files(dest: Path, files: Mapping[str, str]) -> tuple[str, ...]:
    root = Path(dest).resolve()
    written = []
    for path, body in files.items():
        rel = normalize_repo_path(path)
        target = (root / rel).resolve()
        if not str(target).startswith(str(root) + "/") and target != root:
            raise PrivateReviewError("refusing path outside destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        written.append(rel)
    if not written:
        raise PrivateReviewError("test draft requires at least one file")
    return tuple(written)
