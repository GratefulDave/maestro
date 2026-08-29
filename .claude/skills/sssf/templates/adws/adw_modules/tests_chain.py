"""Test-author / test-reviewer / seal payloads as LaneArtifact.

Does not write lane_state. Private bytes stay in the vault object database.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

from . import hidden_vault as hv
from . import private_review as pr
from . import scheduler_types as st

_PYTEST_TOTALS = re.compile(r"(\d+)\s+(passed|failed|errors?|error|skipped|xfailed)")


def _private_blobs(vault: Path, base: str, commit: str) -> tuple[tuple[str, str], ...]:
    before = dict(hv.list_commit_blobs(vault, base))
    after = hv.list_commit_blobs(vault, commit)
    return tuple((path, blob) for path, blob in after if before.get(path) != blob)


def write_test_draft(
    *,
    request: pr.VaultLaneRequest,
    state_root: Path,
    run_repo: Path,
    integration_ref: str,
    files: Mapping[str, str],
    public_contract: Mapping[str, object],
    worktrees_root: Path,
) -> st.LaneArtifact:
    contract = pr.public_contract(
        acceptance_criteria=pr.as_str_tuple(
            public_contract["acceptance_criteria"], "acceptance_criteria"
        ),
        declared_outputs=pr.as_str_tuple(
            public_contract["declared_outputs"], "declared_outputs"
        ),
    )
    vault = hv.ensure_vault(state_root, request.run_id)
    base = hv.seed(vault, run_repo, integration_ref)
    dest = Path(worktrees_root) / "draft-{0}-{1}".format(
        request.lane_id, request.input_digest[:12]
    )
    hv.checkout_vault_worktree(vault, base, dest)
    pr.write_files(dest, files)
    commit = hv.commit_all(dest, "test draft {0}".format(request.input_digest))
    ref = hv.draft_ref(request.run_id, request.lane_id, request.input_digest)
    hv.update_immutable_ref(vault, ref, commit)
    private = _private_blobs(vault, base, commit)
    if not private:
        raise pr.PrivateReviewError("test draft introduced no private blobs")
    private_draft_digest = st.digest_bytes(
        st.canonical_bytes(
            {
                "commit": commit,
                "files": [{"blob": blob, "path": path} for path, blob in private],
            }
        )
    )
    payload = {
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "private_draft_digest": private_draft_digest,
        "private_draft_ref": ref,
        "public_contract": contract,
    }
    private_files = {
        path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in private
    }
    tokens = pr.collect_private_tokens(
        files=private_files,
        vault_path=vault,
        vault_refs=(ref,),
        blob_ids=tuple(blob for _path, blob in private) + (commit,),
    )
    pr.refuse_private_leak(
        payload, tokens, allow=(request.input_digest, private_draft_digest, ref)
    )
    artifact = pr.make_lane_artifact(
        kind=st.ArtifactKind.TEST_DRAFT,
        request=request,
        payload=payload,
        artifact_ref=ref,
    )
    pr.refuse_private_leak(
        st.canonical_bytes(artifact.payload),
        tokens,
        allow=(request.input_digest, private_draft_digest, ref),
    )
    return artifact


def review_test_draft(
    *,
    request: pr.VaultLaneRequest,
    verdict: st.ReviewerVerdict,
    findings: Sequence[Mapping[str, str]] = (),
    test_draft: st.LaneArtifact,
    private_tokens: Sequence[str] = (),
) -> st.LaneArtifact:
    if test_draft.kind is not st.ArtifactKind.TEST_DRAFT:
        raise pr.PrivateReviewError("test review requires TEST_DRAFT")
    findings_out = pr.actionable_findings(verdict, findings, private_tokens)
    payload = {
        "findings": [dict(item) for item in findings_out],
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "verdict": verdict.value,
    }
    if private_tokens:
        pr.refuse_private_leak(payload, private_tokens)
    artifact = pr.make_lane_artifact(
        kind=st.ArtifactKind.TEST_REVIEW,
        request=request,
        payload=payload,
        artifact_ref="test-review:{0}".format(request.input_digest),
        verdict=verdict,
    )
    if private_tokens:
        pr.refuse_private_leak(st.canonical_bytes(artifact.payload), private_tokens)
    return artifact


def seal_accepted_tests(
    *,
    request: pr.VaultLaneRequest,
    state_root: Path,
    run_repo: Path,
    builder_worktree: Path | None,
    test_draft: st.LaneArtifact,
    test_review: st.LaneArtifact,
) -> st.LaneArtifact:
    if test_draft.kind is not st.ArtifactKind.TEST_DRAFT:
        raise pr.PrivateReviewError("seal requires TEST_DRAFT")
    if test_review.kind is not st.ArtifactKind.TEST_REVIEW:
        raise pr.PrivateReviewError("seal requires TEST_REVIEW")
    if test_review.verdict is not st.ReviewerVerdict.PASS:
        raise pr.PrivateReviewError("seal requires TEST_REVIEW PASS")
    vault = hv.ensure_vault(state_root, request.run_id)
    commit = hv.rev_parse(vault, test_draft.artifact_ref)
    base = hv.rev_parse(vault, "refs/maestro/integration-seed")
    private = _private_blobs(vault, base, commit)
    sealed = hv.sealed_ref(request.run_id, request.lane_id, request.input_digest)
    hv.update_immutable_ref(vault, sealed, commit)
    object_ids = (commit,) + tuple(blob for _path, blob in private)
    hv.prove_absent((run_repo,), object_ids)
    if builder_worktree is not None:
        hv.prove_absent((builder_worktree,), object_ids)
        if commit in hv.advertised_object_ids(run_repo):
            raise pr.IsolationError("run repository advertises the sealed commit")
        builder_objects = hv.batch_object_ids(builder_worktree)
        if any(object_id in builder_objects for object_id in object_ids):
            raise pr.IsolationError("builder object database holds sealed tests")
        reachable = hv.rev_list_objects(builder_worktree)
        for object_id in object_ids:
            if object_id in reachable:
                raise pr.IsolationError("builder rev-list reaches private object")
        hv.prove_unfetchable(run_repo, builder_worktree, commit)
        vault_path = hv.vault_path(state_root, request.run_id).resolve()
        builder_root = builder_worktree.resolve()
        if vault_path in builder_root.parents:
            raise pr.IsolationError("vault is inside the builder worktree")
        if str(vault_path).startswith(str(builder_root) + os.sep):
            raise pr.IsolationError("vault is under the builder worktree")
    sealed_digest = st.digest_bytes(
        st.canonical_bytes(
            {
                "commit": commit,
                "files": [{"blob": blob, "path": path} for path, blob in private],
            }
        )
    )
    payload = {
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "public_contract": test_draft.payload["public_contract"],
        "sealed_digest": sealed_digest,
    }
    files = {path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in private}
    tokens = pr.collect_private_tokens(
        files=files,
        vault_path=hv.vault_path(state_root, request.run_id),
        vault_refs=(test_draft.artifact_ref, sealed),
        blob_ids=object_ids,
    )
    pr.refuse_private_leak(payload, tokens, allow=(request.input_digest, sealed_digest))
    artifact = pr.make_lane_artifact(
        kind=st.ArtifactKind.SEALED_TEST_BUNDLE,
        request=request,
        payload=payload,
        artifact_ref=sealed,
    )
    pr.refuse_private_leak(
        st.canonical_bytes(artifact.payload),
        tokens,
        allow=(request.input_digest, sealed_digest),
    )
    return artifact


def sealed_private_files(
    vault: Path, sealed_artifact: st.LaneArtifact
) -> dict[str, str]:
    commit = hv.rev_parse(vault, sealed_artifact.artifact_ref)
    base = hv.rev_parse(vault, "refs/maestro/integration-seed")
    return {path: blob for path, blob in _private_blobs(vault, base, commit)}


def run_private_pytest(
    tree: Path, paths: Sequence[str], timeout_s: float = 120.0
) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree)
    env["COLUMNS"] = "1000"
    env["PYTEST_ADDOPTS"] = ""
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--rootdir",
        str(tree),
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "-q",
        "--tb=no",
        "-rfEs",
        "--",
        *paths,
    ]
    result = subprocess.run(
        cmd,
        cwd=str(tree),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_s,
        check=False,
    )
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    counts = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    summary = ""
    for line in reversed(output.splitlines()):
        if " in " in line and _PYTEST_TOTALS.search(line):
            summary = line
            break
    if summary:
        for count, label in _PYTEST_TOTALS.findall(summary):
            key = "errored" if label.startswith("error") else label
            if key == "xfailed":
                continue
            counts[key] = int(count)
    executed = (
        counts["passed"] + counts["failed"] + counts["errored"] + counts["skipped"]
    )
    return {
        "counts": counts,
        "executed": executed,
        "output": output,
        "returncode": result.returncode,
    }


def draft_private_tokens(
    *,
    state_root: Path,
    run_id: str,
    draft: st.LaneArtifact,
) -> tuple[str, ...]:
    vault = hv.ensure_vault(state_root, run_id)
    commit = hv.rev_parse(vault, draft.artifact_ref)
    base = hv.rev_parse(vault, "refs/maestro/integration-seed")
    private = _private_blobs(vault, base, commit)
    files = {path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in private}
    return pr.collect_private_tokens(
        files=files,
        vault_path=vault,
        vault_refs=(draft.artifact_ref,),
        blob_ids=(commit,) + tuple(blob for _path, blob in private),
    )
