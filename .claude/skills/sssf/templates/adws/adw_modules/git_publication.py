"""Git publication kernel: binding, immutable refs, tree-delta, exact-SHA merge.

Returns payloads and fingerprints. Never writes ``lane_state.stage``.
Stage-store wraps these payloads in the frozen artifact envelope.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from . import scheduler_types as st
from . import workspace_receipt as wr
from .git_helper import BoundGit, GitError, require_oid, zero_oid
from .receipt_crypto import NO_BLOB, NO_PRIOR_REF

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STATUS = re.compile(r"^([AMDTRC])(\d{1,3})?$")


class GitPublicationRefused(RuntimeError):
    """Named Git-publication refusal. Not a lane stage and not a wait."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


@dataclass(frozen=True)
class FilesystemIdentity:
    realpath: str
    device: int
    inode: int

    def payload(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "inode": self.inode,
            "realpath": self.realpath,
        }


@dataclass(frozen=True)
class TargetBinding:
    target_repository_root: str
    target_git_common_dir: str
    target_worktree_git_dir: str
    target_object_format: str
    target_main_ref: str
    target_initial_main_sha: str
    integration_initial_sha: str
    target_repository_fingerprint: str
    target_sync_journal_fingerprint: str
    worktree_root: FilesystemIdentity
    worktree_git_dir: FilesystemIdentity
    git_common_dir: FilesystemIdentity

    def git(self) -> BoundGit:
        return BoundGit(Path(self.target_repository_root))


@dataclass(frozen=True)
class TreeDeltaEntry:
    status: str
    score: int | None
    old_path: str
    new_path: str
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str

    def represented_paths(self) -> tuple[str, ...]:
        paths = []
        if self.old_path:
            paths.append(self.old_path)
        if self.new_path and self.new_path != self.old_path:
            paths.append(self.new_path)
        return tuple(paths)


@dataclass(frozen=True)
class MergeDecision:
    action: Literal["MERGE", "REVALIDATE", "BASE_INVALIDATION"]
    before_sha: str
    candidate_sha: str
    integration_head: str
    builder_base_sha: str


def _identity_of(path: str) -> FilesystemIdentity:
    real = os.path.realpath(path)
    st = os.lstat(real)
    if stat.S_ISLNK(st.st_mode):
        raise GitPublicationRefused("TARGET_SYMLINK", real)
    if not stat.S_ISDIR(st.st_mode):
        raise GitPublicationRefused("TARGET_NOT_DIRECTORY", real)
    return FilesystemIdentity(realpath=real, device=st.st_dev, inode=st.st_ino)


def _require_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise GitPublicationRefused("INVALID_ID", f"{label}:{value!r}")
    return value


def _require_digest(value: str, label: str) -> str:
    try:
        return st.require_hex_digest(value, name=label)
    except st.CanonicalIdentityError as exc:
        raise GitPublicationRefused("INVALID_DIGEST", str(exc)) from exc


def _require_ref_name(ref: str) -> str:
    if not ref.startswith("refs/") or ".." in ref or ref.endswith("/") or "//" in ref:
        raise GitPublicationRefused("INVALID_REF", ref)
    return ref


def integration_ref_name(run_id: str) -> str:
    return st.integration_ref(_require_id(run_id, "run_id"))


def candidate_ref_name(run_id: str, lane_id: str, input_digest: str) -> str:
    return st.candidate_ref(
        _require_id(run_id, "run_id"),
        _require_id(lane_id, "lane_id"),
        _require_digest(input_digest, "input_digest"),
    )


def publication_ref_name(run_id: str, review_input_fingerprint: str) -> str:
    return st.publication_ref(
        _require_id(run_id, "run_id"),
        _require_digest(review_input_fingerprint, "review_input_fingerprint"),
    )


def target_repository_fingerprint_payload(
    worktree_root: FilesystemIdentity,
    worktree_git_dir: FilesystemIdentity,
    git_common_dir: FilesystemIdentity,
    object_format: str,
) -> dict[str, Any]:
    return {
        "git_common_dir": git_common_dir.payload(),
        "object_format": object_format,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
        "worktree_git_dir": worktree_git_dir.payload(),
        "worktree_root": worktree_root.payload(),
    }


def revalidate_binding(binding: TargetBinding) -> None:
    root = _identity_of(binding.target_repository_root)
    git_dir = _identity_of(binding.target_worktree_git_dir)
    common = _identity_of(binding.target_git_common_dir)
    if (
        root != binding.worktree_root
        or git_dir != binding.worktree_git_dir
        or common != binding.git_common_dir
    ):
        raise GitPublicationRefused("TARGET_MOVED", binding.target_repository_root)
    git = binding.git()
    if git.object_format() != binding.target_object_format:
        raise GitPublicationRefused("TARGET_MOVED", "object_format")
    payload = target_repository_fingerprint_payload(
        root, git_dir, common, binding.target_object_format
    )
    if st.digest_canonical(payload) != binding.target_repository_fingerprint:
        raise GitPublicationRefused("TARGET_MOVED", "fingerprint")


@contextmanager
def target_worktree_lock(worktree_git_dir: str) -> Iterator[None]:
    path = os.path.join(worktree_git_dir, "maestro-publication.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GitPublicationRefused(
                "PUBLICATION_WORKTREE_LOCK_REFUSED", worktree_git_dir
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def bind_target_worktree(repo: str | Path, main_ref: str) -> TargetBinding:
    _require_ref_name(main_ref)
    proposed = Path(repo)
    if not proposed.is_absolute():
        raise GitPublicationRefused("TARGET_NOT_ABSOLUTE", str(proposed))
    root = _identity_of(str(proposed))
    git = BoundGit(Path(root.realpath))
    try:
        if git.is_bare():
            raise GitPublicationRefused("TARGET_BARE", root.realpath)
        head = git.symbolic_head()
        if head != main_ref:
            raise GitPublicationRefused("TARGET_HEAD_MISMATCH", f"{head}!={main_ref}")
        main_sha = git.rev_parse(main_ref)
        object_format = git.object_format()
        git_dir = _identity_of(str(git.git_dir()))
        common = _identity_of(str(git.git_common_dir()))
    except GitError as exc:
        raise GitPublicationRefused(exc.code, exc.detail) from exc
    payload = target_repository_fingerprint_payload(
        root, git_dir, common, object_format
    )
    fingerprint = st.digest_canonical(payload)
    with target_worktree_lock(git_dir.realpath):
        _git_fd, _journal_fd, journal_fp, _path = wr.ensure_journal_root(
            git_dir.realpath,
            manifest_parent_devices=(root.device, git_dir.device, common.device),
        )
        os.close(_journal_fd)
        os.close(_git_fd)
    return TargetBinding(
        target_repository_root=root.realpath,
        target_git_common_dir=common.realpath,
        target_worktree_git_dir=git_dir.realpath,
        target_object_format=object_format,
        target_main_ref=main_ref,
        target_initial_main_sha=main_sha,
        integration_initial_sha=main_sha,
        target_repository_fingerprint=fingerprint,
        target_sync_journal_fingerprint=journal_fp,
        worktree_root=root,
        worktree_git_dir=git_dir,
        git_common_dir=common,
    )


def ensure_integration_ref(
    binding: TargetBinding, run_id: str, expected_tip: str
) -> dict[str, Any]:
    revalidate_binding(binding)
    ref = integration_ref_name(run_id)
    git = binding.git()
    initial = require_oid(
        binding.integration_initial_sha, object_format=binding.target_object_format
    )
    expected = require_oid(expected_tip, object_format=binding.target_object_format)
    current = git.read_ref(ref)
    zero = zero_oid(binding.target_object_format)
    if current is None:
        if expected != initial:
            raise GitPublicationRefused(
                "INTEGRATION_REF_MISSING", f"{expected}!={initial}"
            )
        git.update_ref(ref, initial, zero)
        current = git.read_ref(ref)
    if current != expected:
        raise GitPublicationRefused(
            "INTEGRATION_REF_COLLISION", f"{current}!={expected}"
        )
    return {
        "integration_ref": ref,
        "sha": current,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
    }


def pin_candidate_ref(
    binding: TargetBinding,
    *,
    run_id: str,
    lane_id: str,
    input_digest: str,
    candidate_sha: str,
) -> dict[str, Any]:
    revalidate_binding(binding)
    ref = candidate_ref_name(run_id, lane_id, input_digest)
    git = binding.git()
    sha = require_oid(candidate_sha, object_format=binding.target_object_format)
    current = git.read_ref(ref)
    if current is None:
        git.update_ref(ref, sha, zero_oid(binding.target_object_format))
        current = sha
    elif current != sha:
        raise GitPublicationRefused("CANDIDATE_REF_COLLISION", f"{current}!={sha}")
    return {
        "candidate_ref": ref,
        "candidate_sha": current,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
    }


def parse_diff_tree(raw: bytes) -> list[TreeDeltaEntry]:
    entries: list[TreeDeltaEntry] = []
    if not raw:
        return entries
    data = raw.split(b"\0")
    i = 0
    while i < len(data):
        chunk = data[i]
        if not chunk:
            i += 1
            continue
        if not chunk.startswith(b":"):
            raise GitPublicationRefused(
                "TREE_DELTA_PARSE", chunk.decode("utf-8", "replace")
            )
        meta = chunk[1:].decode("ascii")
        old_mode, new_mode, old_oid, new_oid, status_field = meta.split(" ")
        match = _STATUS.fullmatch(status_field)
        if match is None:
            raise GitPublicationRefused("TREE_DELTA_PARSE", status_field)
        status = match.group(1)
        score = int(match.group(2)) if match.group(2) else None
        i += 1
        if i >= len(data):
            raise GitPublicationRefused("TREE_DELTA_PARSE", "missing path")
        first_path = data[i].decode("utf-8")
        i += 1
        old_path = first_path
        new_path = first_path
        if status in {"R", "C"}:
            if i >= len(data):
                raise GitPublicationRefused("TREE_DELTA_PARSE", "missing rename path")
            new_path = data[i].decode("utf-8")
            i += 1
            if status == "C":
                old_path = first_path
            else:
                old_path = first_path
        if status == "A":
            old_path = ""
            old_oid = NO_BLOB
            old_mode = "000000"
        if status == "D":
            new_path = ""
            new_oid = NO_BLOB
            new_mode = "000000"
        entries.append(
            TreeDeltaEntry(
                status=status,
                score=score,
                old_path=old_path,
                new_path=new_path,
                old_mode=old_mode,
                new_mode=new_mode,
                old_oid=old_oid,
                new_oid=new_oid,
            )
        )
    return entries


def measure_tree_delta(
    binding: TargetBinding, base_sha: str, candidate_sha: str
) -> list[TreeDeltaEntry]:
    git = binding.git()
    raw = git.diff_tree_raw(base_sha, candidate_sha)
    return parse_diff_tree(raw)


def validate_declared_ownership(
    delta: Sequence[TreeDeltaEntry],
    declared_outputs: Sequence[str],
    *,
    changed: bool,
) -> None:
    allowed = set(declared_outputs)
    if changed and not delta:
        raise GitPublicationRefused(
            "CANDIDATE_OUTPUT_OWNERSHIP_REFUSED", "changed=true empty delta"
        )
    if not changed and delta:
        raise GitPublicationRefused(
            "CANDIDATE_OUTPUT_OWNERSHIP_REFUSED", "changed=false nonempty delta"
        )
    for entry in delta:
        if entry.old_mode == "160000" or entry.new_mode == "160000":
            raise GitPublicationRefused("CANDIDATE_OUTPUT_OWNERSHIP_REFUSED", "gitlink")
        for path in entry.represented_paths():
            if path not in allowed:
                raise GitPublicationRefused("CANDIDATE_OUTPUT_OWNERSHIP_REFUSED", path)


def candidate_output_payload(
    *,
    candidate_ref: str,
    candidate_sha: str,
    builder_base_sha: str,
    changed: bool,
    delta: Sequence[TreeDeltaEntry],
) -> dict[str, Any]:
    return {
        "builder_base_sha": builder_base_sha,
        "candidate_ref": candidate_ref,
        "candidate_sha": candidate_sha,
        "changed": changed,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
        "tree_delta": [
            {
                "new_mode": item.new_mode,
                "new_oid": item.new_oid,
                "new_path": item.new_path or "NO_BLOB",
                "old_mode": item.old_mode,
                "old_oid": item.old_oid,
                "old_path": item.old_path or "NO_BLOB",
                "score": item.score if item.score is not None else 0,
                "status": item.status,
            }
            for item in delta
        ],
    }


def admit_candidate(
    binding: TargetBinding,
    *,
    run_id: str,
    lane_id: str,
    input_digest: str,
    builder_base_sha: str,
    candidate_sha: str,
    changed: bool,
    declared_outputs: Sequence[str],
) -> dict[str, Any]:
    revalidate_binding(binding)
    git = binding.git()
    base = require_oid(builder_base_sha, object_format=binding.target_object_format)
    sha = require_oid(candidate_sha, object_format=binding.target_object_format)
    if not changed and sha != base:
        raise GitPublicationRefused(
            "CANDIDATE_OUTPUT_OWNERSHIP_REFUSED", "changed=false sha!=base"
        )
    if changed:
        parents = git.commit_parents(sha)
        if not parents or parents[0] != base:
            raise GitPublicationRefused(
                "CANDIDATE_WRONG_FIRST_PARENT", ",".join(parents)
            )
    delta = measure_tree_delta(binding, base, sha)
    validate_declared_ownership(delta, declared_outputs, changed=changed)
    pin = pin_candidate_ref(
        binding,
        run_id=run_id,
        lane_id=lane_id,
        input_digest=input_digest,
        candidate_sha=sha,
    )
    payload = candidate_output_payload(
        candidate_ref=pin["candidate_ref"],
        candidate_sha=sha,
        builder_base_sha=base,
        changed=changed,
        delta=delta,
    )
    return payload


def reconcile_candidate_ref(
    binding: TargetBinding,
    *,
    run_id: str,
    lane_id: str,
    input_digest: str,
    builder_base_sha: str,
    changed: bool,
    declared_outputs: Sequence[str],
) -> dict[str, Any]:
    revalidate_binding(binding)
    ref = candidate_ref_name(run_id, lane_id, input_digest)
    git = binding.git()
    current = git.read_ref(ref)
    if current is None:
        raise GitPublicationRefused("CANDIDATE_REF_MISSING", ref)
    return admit_candidate(
        binding,
        run_id=run_id,
        lane_id=lane_id,
        input_digest=input_digest,
        builder_base_sha=builder_base_sha,
        candidate_sha=current,
        changed=changed,
        declared_outputs=declared_outputs,
    )


def decide_merge_action(
    *,
    changed: bool,
    builder_base_sha: str,
    candidate_sha: str,
    integration_head: str,
) -> MergeDecision:
    if not changed and builder_base_sha != integration_head:
        return MergeDecision(
            action="BASE_INVALIDATION",
            before_sha=integration_head,
            candidate_sha=candidate_sha,
            integration_head=integration_head,
            builder_base_sha=builder_base_sha,
        )
    if not changed:
        if candidate_sha != integration_head:
            raise GitPublicationRefused(
                "CANDIDATE_OUTPUT_OWNERSHIP_REFUSED",
                "zero-delta candidate must equal integration HEAD",
            )
        return MergeDecision(
            action="REVALIDATE",
            before_sha=integration_head,
            candidate_sha=candidate_sha,
            integration_head=integration_head,
            builder_base_sha=builder_base_sha,
        )
    return MergeDecision(
        action="MERGE",
        before_sha=integration_head,
        candidate_sha=candidate_sha,
        integration_head=integration_head,
        builder_base_sha=builder_base_sha,
    )


def base_invalidation_payload(
    *,
    stale_builder_output_artifact_id: str,
    stale_code_review_artifact_id: str,
    stale_builder_base_sha: str,
    stale_candidate_sha: str,
    observed_integration_head: str,
    input_digest: str,
) -> dict[str, Any]:
    return {
        "input_digest": input_digest,
        "kind": "BASE_INVALIDATION",
        "observed_integration_head": observed_integration_head,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
        "stale_builder_base_sha": stale_builder_base_sha,
        "stale_builder_output_artifact_id": stale_builder_output_artifact_id,
        "stale_candidate_sha": stale_candidate_sha,
        "stale_code_review_artifact_id": stale_code_review_artifact_id,
    }


def canonical_merge_message(
    *,
    run_id: str,
    lane_id: str,
    stage_input_digest: str,
    builder_artifact_id: str,
    before_sha: str,
    candidate_sha: str,
    expected_tree_sha: str,
) -> bytes:
    return (
        f"run_id: {run_id}\n"
        f"lane_id: {lane_id}\n"
        f"stage_input_digest: {stage_input_digest}\n"
        f"builder_artifact_id: {builder_artifact_id}\n"
        f"before_sha: {before_sha}\n"
        f"candidate_sha: {candidate_sha}\n"
        f"expected_tree_sha: {expected_tree_sha}\n"
    ).encode("utf-8")


def integration_merge_payload(
    *,
    builder_output_artifact_id: str,
    code_review_artifact_id: str,
    builder_base_sha: str,
    candidate_ref: str,
    candidate_sha: str,
    before_sha: str,
    after_sha: str,
    expected_tree_sha: str,
    revalidated: bool,
    input_digest: str,
) -> dict[str, Any]:
    return {
        "after_sha": after_sha,
        "before_sha": before_sha,
        "builder_base_sha": builder_base_sha,
        "builder_output_artifact_id": builder_output_artifact_id,
        "candidate_ref": candidate_ref,
        "candidate_sha": candidate_sha,
        "code_review_artifact_id": code_review_artifact_id,
        "expected_tree_sha": expected_tree_sha,
        "input_digest": input_digest,
        "kind": "INTEGRATION_MERGE",
        "revalidated": revalidated,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
    }


def _expected_merge_commit(
    git: BoundGit,
    *,
    run_id: str,
    lane_id: str,
    stage_input_digest: str,
    builder_artifact_id: str,
    candidate_sha: str,
    before_sha: str,
    epoch_seconds: int,
) -> tuple[str, str, bytes]:
    try:
        tree = git.merge_tree_write_tree(before_sha, candidate_sha)
    except GitError as exc:
        raise GitPublicationRefused(exc.code, exc.detail) from exc
    message = canonical_merge_message(
        run_id=run_id,
        lane_id=lane_id,
        stage_input_digest=stage_input_digest,
        builder_artifact_id=builder_artifact_id,
        before_sha=before_sha,
        candidate_sha=candidate_sha,
        expected_tree_sha=tree,
    )
    after = git.commit_tree(
        tree, (before_sha, candidate_sha), message, epoch_seconds=epoch_seconds
    )
    return after, tree, message


def execute_exact_sha_merge(
    binding: TargetBinding,
    *,
    run_id: str,
    lane_id: str,
    stage_input_digest: str,
    builder_artifact_id: str,
    code_review_artifact_id: str,
    builder_base_sha: str,
    candidate_ref: str,
    candidate_sha: str,
    before_sha: str,
    epoch_seconds: int,
    input_digest: str,
) -> dict[str, Any]:
    revalidate_binding(binding)
    git = binding.git()
    ref = integration_ref_name(run_id)
    head = git.read_ref(ref)
    if head != before_sha:
        raise GitPublicationRefused(
            "INTEGRATION_HEAD_MISMATCH", f"{head}!={before_sha}"
        )
    pinned = git.read_ref(candidate_ref)
    if pinned != candidate_sha:
        raise GitPublicationRefused(
            "CANDIDATE_REF_MISMATCH", f"{pinned}!={candidate_sha}"
        )
    after, tree, _message = _expected_merge_commit(
        git,
        run_id=run_id,
        lane_id=lane_id,
        stage_input_digest=stage_input_digest,
        builder_artifact_id=builder_artifact_id,
        candidate_sha=candidate_sha,
        before_sha=before_sha,
        epoch_seconds=epoch_seconds,
    )
    try:
        git.update_ref(ref, after, before_sha)
    except GitError as exc:
        raise GitPublicationRefused("INTEGRATION_CAS_REFUSED", exc.detail) from exc
    return integration_merge_payload(
        builder_output_artifact_id=builder_artifact_id,
        code_review_artifact_id=code_review_artifact_id,
        builder_base_sha=builder_base_sha,
        candidate_ref=candidate_ref,
        candidate_sha=candidate_sha,
        before_sha=before_sha,
        after_sha=after,
        expected_tree_sha=tree,
        revalidated=False,
        input_digest=input_digest,
    )


def merge_or_reconcile(
    binding: TargetBinding,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        return execute_exact_sha_merge(binding, **kwargs)
    except GitPublicationRefused as exc:
        if exc.code in {"INTEGRATION_CAS_REFUSED", "INTEGRATION_HEAD_MISMATCH"}:
            return reconcile_integration_merge(binding, **kwargs)
        raise


def revalidate_zero_delta(
    binding: TargetBinding,
    *,
    builder_artifact_id: str,
    code_review_artifact_id: str,
    builder_base_sha: str,
    candidate_ref: str,
    candidate_sha: str,
    input_digest: str,
) -> dict[str, Any]:
    revalidate_binding(binding)
    git = binding.git()
    tree = git.commit_tree_oid(candidate_sha)
    return integration_merge_payload(
        builder_output_artifact_id=builder_artifact_id,
        code_review_artifact_id=code_review_artifact_id,
        builder_base_sha=builder_base_sha,
        candidate_ref=candidate_ref,
        candidate_sha=candidate_sha,
        before_sha=candidate_sha,
        after_sha=candidate_sha,
        expected_tree_sha=tree,
        revalidated=True,
        input_digest=input_digest,
    )


def reconcile_integration_merge(
    binding: TargetBinding,
    *,
    run_id: str,
    lane_id: str,
    stage_input_digest: str,
    builder_artifact_id: str,
    code_review_artifact_id: str,
    builder_base_sha: str,
    candidate_ref: str,
    candidate_sha: str,
    before_sha: str,
    epoch_seconds: int,
    input_digest: str,
) -> dict[str, Any]:
    revalidate_binding(binding)
    git = binding.git()
    ref = integration_ref_name(run_id)
    head = git.read_ref(ref)
    if head is None:
        raise GitPublicationRefused(
            "INTEGRATION_HEAD_MISMATCH", f"{head}!={before_sha}"
        )
    pinned = git.read_ref(candidate_ref)
    if pinned != candidate_sha:
        raise GitPublicationRefused(
            "CANDIDATE_REF_MISMATCH", f"{pinned}!={candidate_sha}"
        )
    if head == before_sha:
        return execute_exact_sha_merge(
            binding,
            run_id=run_id,
            lane_id=lane_id,
            stage_input_digest=stage_input_digest,
            builder_artifact_id=builder_artifact_id,
            code_review_artifact_id=code_review_artifact_id,
            builder_base_sha=builder_base_sha,
            candidate_ref=candidate_ref,
            candidate_sha=candidate_sha,
            before_sha=before_sha,
            epoch_seconds=epoch_seconds,
            input_digest=input_digest,
        )
    expected, tree, message = _expected_merge_commit(
        git,
        run_id=run_id,
        lane_id=lane_id,
        stage_input_digest=stage_input_digest,
        builder_artifact_id=builder_artifact_id,
        candidate_sha=candidate_sha,
        before_sha=before_sha,
        epoch_seconds=epoch_seconds,
    )
    parents = git.commit_parents(head)
    if (
        head != expected
        or parents != (before_sha, candidate_sha)
        or git.commit_tree_oid(head) != tree
        or git.commit_message(head) != message
    ):
        raise GitPublicationRefused("INTEGRATION_REF_COLLISION", f"{head}!={expected}")
    return integration_merge_payload(
        builder_output_artifact_id=builder_artifact_id,
        code_review_artifact_id=code_review_artifact_id,
        builder_base_sha=builder_base_sha,
        candidate_ref=candidate_ref,
        candidate_sha=candidate_sha,
        before_sha=before_sha,
        after_sha=head,
        expected_tree_sha=tree,
        revalidated=False,
        input_digest=input_digest,
    )


def durable_integration_tip(
    integration_initial_sha: str,
    merges: Sequence[Mapping[str, Any]],
) -> str:
    tip = integration_initial_sha
    for record in merges:
        if record.get("revalidated"):
            if record["before_sha"] != tip or record["after_sha"] != tip:
                raise GitPublicationRefused(
                    "INTEGRATION_RECEIPT_REFUSED", "revalidation not anchored"
                )
            continue
        if record["before_sha"] != tip:
            raise GitPublicationRefused(
                "INTEGRATION_RECEIPT_REFUSED", "merge chain break"
            )
        tip = record["after_sha"]
    return tip


def final_review_input_fingerprint(
    *,
    integration_sha: str,
    plan_revision: int,
    plan_digest: str,
    lanes: Sequence[Mapping[str, str]],
) -> str:
    return st.final_review_input_fingerprint(
        integration_sha=integration_sha,
        plan_revision=plan_revision,
        plan_digest=plan_digest,
        lanes=lanes,
    )


def publication_receipt_payload(
    *,
    run_id: str,
    target_repository_fingerprint: str,
    target_main_ref: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any]:
    return {
        "expected_before_sha": expected_before_sha,
        "final_review_artifact_id": final_review_artifact_id,
        "kind": "MAIN_PUBLICATION_RECEIPT",
        "review_input_fingerprint": review_input_fingerprint,
        "reviewed_integration_sha": reviewed_integration_sha,
        "run_id": run_id,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
        "target_main_ref": target_main_ref,
        "target_repository_fingerprint": target_repository_fingerprint,
    }


def main_publication_payload(
    *,
    review_input_fingerprint: str,
    receipt_ref: str,
    receipt_object: str,
    expected_before_sha: str,
    published_sha: str,
) -> dict[str, Any]:
    return {
        "expected_before_sha": expected_before_sha,
        "input_digest": review_input_fingerprint,
        "kind": "MAIN_PUBLICATION",
        "published_sha": published_sha,
        "receipt_object": receipt_object,
        "receipt_ref": receipt_ref,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
    }


def _open_journal(
    binding: TargetBinding, run_id: str, fingerprint: str, *, create: bool
) -> wr.PublicationJournal:
    return wr.open_run_journal(
        binding.target_worktree_git_dir,
        run_id,
        fingerprint,
        expected_fingerprint=binding.target_sync_journal_fingerprint,
        create=create,
    )


def _try_open_journal(
    binding: TargetBinding, run_id: str, fingerprint: str
) -> wr.PublicationJournal | None:
    try:
        return _open_journal(binding, run_id, fingerprint, create=False)
    except wr.JournalError as exc:
        if exc.code == "JOURNAL_MISSING":
            return None
        raise GitPublicationRefused(exc.code, exc.detail) from exc


def _receipt_from_kwargs(
    binding: TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any]:
    return publication_receipt_payload(
        run_id=run_id,
        target_repository_fingerprint=binding.target_repository_fingerprint,
        target_main_ref=binding.target_main_ref,
        review_input_fingerprint=review_input_fingerprint,
        final_review_artifact_id=final_review_artifact_id,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )


def _require_journal_receipt(
    journal: wr.PublicationJournal, expected: Mapping[str, Any]
) -> bytes:
    stored = wr.read_json_at(journal.run_fd, "receipt.json")
    stored_bytes = st.canonical_bytes(dict(stored))
    expected_bytes = st.canonical_bytes(dict(expected))
    if stored_bytes != expected_bytes:
        raise GitPublicationRefused(
            "PUBLICATION_RECEIPT_COLLISION", journal.run_dir_name
        )
    return expected_bytes


def _preflight_publication(
    binding: TargetBinding,
    *,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> None:
    revalidate_binding(binding)
    git = binding.git()
    if git.is_bare():
        raise GitPublicationRefused("TARGET_BARE", binding.target_repository_root)
    try:
        head = git.symbolic_head()
    except GitError as exc:
        raise GitPublicationRefused("TARGET_DETACHED", exc.detail) from exc
    if head != binding.target_main_ref:
        raise GitPublicationRefused(
            "TARGET_HEAD_MISMATCH", f"{head}!={binding.target_main_ref}"
        )
    main_sha = git.rev_parse(binding.target_main_ref)
    if main_sha != expected_before_sha:
        raise GitPublicationRefused(
            "PUBLICATION_PREFLIGHT_REFUSED", f"main {main_sha} != expected-before"
        )
    if expected_before_sha != reviewed_integration_sha and not git.is_ancestor(
        expected_before_sha, reviewed_integration_sha
    ):
        raise GitPublicationRefused(
            "PUBLICATION_NOT_FAST_FORWARD", reviewed_integration_sha
        )
    if not git.diff_cached_quiet(
        expected_before_sha, git_dir=binding.target_worktree_git_dir
    ):
        raise GitPublicationRefused("PUBLICATION_PREFLIGHT_REFUSED", "index dirty")
    if not git.diff_files_quiet(git_dir=binding.target_worktree_git_dir):
        raise GitPublicationRefused("PUBLICATION_PREFLIGHT_REFUSED", "worktree dirty")
    untracked = git.ls_others(ignored=False, git_dir=binding.target_worktree_git_dir)
    if untracked:
        raise GitPublicationRefused(
            "PUBLICATION_PREFLIGHT_REFUSED", "untracked:" + ",".join(untracked)
        )
    ignored = git.ls_others(ignored=True, git_dir=binding.target_worktree_git_dir)
    if ignored:
        raise GitPublicationRefused(
            "PUBLICATION_PREFLIGHT_REFUSED", "ignored:" + ",".join(ignored)
        )


def prepare_publication_journal(
    binding: TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> wr.PublicationJournal:
    git = binding.git()
    receipt = _receipt_from_kwargs(
        binding,
        run_id=run_id,
        review_input_fingerprint=review_input_fingerprint,
        final_review_artifact_id=final_review_artifact_id,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )
    owner = {
        "run_id": run_id,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
        "target_sync_journal_fingerprint": binding.target_sync_journal_fingerprint,
        "target_worktree_git_dir": binding.target_worktree_git_dir,
    }
    journal = _open_journal(binding, run_id, review_input_fingerprint, create=True)
    if wr.journal_is_published(journal):
        _require_journal_receipt(journal, receipt)
        return journal
    wr.write_owner_and_receipt(journal, owner, receipt)
    _require_journal_receipt(journal, receipt)

    old_entries = wr.parse_ls_tree(git.ls_tree_z(expected_before_sha))
    new_entries = wr.parse_ls_tree(git.ls_tree_z(reviewed_integration_sha))
    leaves = wr.build_manifest(old_entries, new_entries)
    root_fd = wr.open_directory_nofollow(binding.target_repository_root)
    try:
        directories = wr.snapshot_directories(root_fd, [leaf.path for leaf in leaves])
    finally:
        os.close(root_fd)
    wr.write_manifest(journal, leaves, directories)
    wr.store_reviewed_blobs(journal, git, leaves)
    wr.prepare_reviewed_index(
        journal, git, reviewed_integration_sha, binding.target_worktree_git_dir
    )
    return journal


def cas_receipt_and_main(
    binding: TargetBinding,
    journal: wr.PublicationJournal,
    *,
    run_id: str,
    review_input_fingerprint: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> tuple[str, str]:
    git = binding.git()
    receipt = wr.read_json_at(journal.run_fd, "receipt.json")
    receipt_bytes = st.canonical_bytes(receipt)
    receipt_oid = git.hash_object(receipt_bytes, object_type="blob", write=True)
    ref = publication_ref_name(run_id, review_input_fingerprint)
    zero = zero_oid(binding.target_object_format)
    script = (
        "start\n"
        f"update {ref} {receipt_oid} {zero}\n"
        f"update {binding.target_main_ref} {reviewed_integration_sha} {expected_before_sha}\n"
        "commit\n"
    )
    try:
        git.update_ref_stdin(script)
        return ref, receipt_oid
    except GitError as exc:
        existing = git.read_ref(ref)
        if existing is None:
            raise GitPublicationRefused(
                "PUBLICATION_REF_CAS_REFUSED", exc.detail or NO_PRIOR_REF
            ) from exc
        current = git.cat_file("blob", existing)
        if current != receipt_bytes or existing != receipt_oid:
            raise GitPublicationRefused("PUBLICATION_RECEIPT_COLLISION", ref) from exc
        main_sha = git.rev_parse(binding.target_main_ref)
        if main_sha == reviewed_integration_sha:
            return ref, receipt_oid
        raise GitPublicationRefused("PUBLICATION_REF_CAS_REFUSED", exc.detail) from exc


def _verify_postconditions(
    binding: TargetBinding, *, reviewed_integration_sha: str
) -> None:
    revalidate_binding(binding)
    git = binding.git()
    if git.symbolic_head() != binding.target_main_ref:
        raise GitPublicationRefused("TARGET_HEAD_MISMATCH", git.symbolic_head())
    if git.rev_parse(binding.target_main_ref) != reviewed_integration_sha:
        raise GitPublicationRefused("PUBLICATION_POSTCONDITION", "main != reviewed")
    if not git.diff_cached_quiet(
        reviewed_integration_sha, git_dir=binding.target_worktree_git_dir
    ):
        raise GitPublicationRefused("PUBLICATION_POSTCONDITION", "index")
    if not git.diff_files_quiet(git_dir=binding.target_worktree_git_dir):
        raise GitPublicationRefused("PUBLICATION_POSTCONDITION", "worktree")
    if git.ls_others(
        ignored=False, git_dir=binding.target_worktree_git_dir
    ) or git.ls_others(ignored=True, git_dir=binding.target_worktree_git_dir):
        raise GitPublicationRefused("PUBLICATION_POSTCONDITION", "untracked")


def _install_reviewed_worktree(
    binding: TargetBinding,
    journal: wr.PublicationJournal,
    *,
    reviewed_integration_sha: str,
) -> None:
    try:
        _verify_postconditions(
            binding, reviewed_integration_sha=reviewed_integration_sha
        )
        return
    except GitPublicationRefused:
        pass
    wr.create_index_lock(binding.target_worktree_git_dir, journal)
    wr.synchronize_publication_worktree(
        target_root=binding.target_repository_root,
        worktree_git_dir=binding.target_worktree_git_dir,
        journal=journal,
        object_format=binding.target_object_format,
        git=binding.git(),
    )
    _verify_postconditions(binding, reviewed_integration_sha=reviewed_integration_sha)


def _complete_started_publication(
    binding: TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any]:
    journal = prepare_publication_journal(
        binding,
        run_id=run_id,
        review_input_fingerprint=review_input_fingerprint,
        final_review_artifact_id=final_review_artifact_id,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )
    try:
        wr.publish_journal(journal)
        ref, receipt_oid = cas_receipt_and_main(
            binding,
            journal,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
        _install_reviewed_worktree(
            binding, journal, reviewed_integration_sha=reviewed_integration_sha
        )
        return main_publication_payload(
            review_input_fingerprint=review_input_fingerprint,
            receipt_ref=ref,
            receipt_object=receipt_oid,
            expected_before_sha=expected_before_sha,
            published_sha=reviewed_integration_sha,
        )
    finally:
        wr.close_journal(journal)


def reconcile_publication_if_present_locked(
    binding: TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any] | None:
    """Reconcile without acquiring the worktree lock. Caller already holds it."""
    revalidate_binding(binding)
    git = binding.git()
    ref = publication_ref_name(run_id, review_input_fingerprint)
    receipt_sha = git.read_ref(ref)
    main_sha = git.rev_parse(binding.target_main_ref)
    journal = _try_open_journal(binding, run_id, review_input_fingerprint)
    published = False
    if journal is not None:
        try:
            published = wr.journal_is_published(journal)
        finally:
            wr.close_journal(journal)
    if receipt_sha is None and main_sha == reviewed_integration_sha:
        raise GitPublicationRefused(
            "PUBLICATION_EXTERNAL_SAME_SHA", reviewed_integration_sha
        )
    if receipt_sha is None and not published:
        return None
    if receipt_sha is not None:
        expected = _receipt_from_kwargs(
            binding,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            final_review_artifact_id=final_review_artifact_id,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
        current = git.cat_file("blob", receipt_sha)
        if current != st.canonical_bytes(expected):
            raise GitPublicationRefused("PUBLICATION_RECEIPT_COLLISION", ref)
        if main_sha not in {expected_before_sha, reviewed_integration_sha}:
            raise GitPublicationRefused("PUBLICATION_EXTERNAL_MISMATCH", main_sha)
    return _complete_started_publication(
        binding,
        run_id=run_id,
        review_input_fingerprint=review_input_fingerprint,
        final_review_artifact_id=final_review_artifact_id,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )


def publish_or_reconcile_locked(
    binding: TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any]:
    """Publish without acquiring the worktree lock. Caller already holds it."""
    existing = reconcile_publication_if_present_locked(
        binding,
        run_id=run_id,
        review_input_fingerprint=review_input_fingerprint,
        final_review_artifact_id=final_review_artifact_id,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )
    if existing is not None:
        return existing
    _preflight_publication(
        binding,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )
    return _complete_started_publication(
        binding,
        run_id=run_id,
        review_input_fingerprint=review_input_fingerprint,
        final_review_artifact_id=final_review_artifact_id,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )


def reconcile_publication_if_present(
    binding: TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any] | None:
    with target_worktree_lock(binding.target_worktree_git_dir):
        return reconcile_publication_if_present_locked(
            binding,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            final_review_artifact_id=final_review_artifact_id,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )


def publish_or_reconcile(
    binding: TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any]:
    with target_worktree_lock(binding.target_worktree_git_dir):
        return publish_or_reconcile_locked(
            binding,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            final_review_artifact_id=final_review_artifact_id,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
