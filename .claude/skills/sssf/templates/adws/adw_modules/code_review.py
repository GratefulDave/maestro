"""Builder view and CODE_REVIEW LaneArtifact. Never writes lane_state.

The builder receives the public contract, architecture constraints, allowed
paths, prior redacted review, and sealed digest. It does not receive private
source, fixtures, selectors, expected literals, vault paths, or private bytes.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Mapping, Sequence

from . import hidden_vault as hv
from . import private_review as pr
from . import scheduler_types as st
from . import tests_chain as tc

_RUNNER_REVISE = {
    "implementation_area": "declared product outputs",
    "observed_behavior": "sealed private tests failed, errored, or did not execute",
    "required_behavior": "the candidate must pass every sealed private test",
    "violated_requirement": "accepted sealed tests bind the candidate",
}

def _review_tree(repo: Path, sha: str, dest: Path) -> Path:
    dest = Path(dest)
    if dest.exists() and dest.is_dir() and any(dest.iterdir()):
        return hv.refresh_materialized_commit(repo, sha, dest)
    return hv.materialize_commit(repo, sha, dest)



def _refuse_candidate_private_collisions(
    dest: Path, files: Mapping[str, str]
) -> None:
    root = Path(dest).resolve()
    for path in sorted(files):
        rel = pr.normalize_repo_path(path)
        target = root.joinpath(*rel.split("/"))
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            raise pr.IsolationError(
                "sealed private path escapes review tree: {0}".format(rel)
            )
        if resolved == root or target.exists() or target.is_symlink():
            raise pr.PrivatePathCollisionError(rel)


def detect_candidate_private_collisions(
    *,
    request: pr.VaultLaneRequest,
    state_root: Path,
    candidate_repo: Path,
    candidate_sha: str,
    sealed_bundle: st.LaneArtifact,
    scratch_root: Path,
) -> dict[str, str]:
    vault = hv.ensure_vault(state_root, request.run_id)
    files = tc.sealed_private_files(vault, sealed_bundle)
    if not files:
        raise pr.PrivateReviewError("sealed bundle has no private tests")
    dest = Path(scratch_root) / "review-{0}-{1}".format(
        request.lane_id, request.input_digest[:12]
    )
    _review_tree(candidate_repo, candidate_sha, dest)
    _refuse_candidate_private_collisions(dest, files)
    return files


def test_invalidation_payload(
    *,
    input_digest: str,
    input_artifact_ids: Sequence[str],
    collision: pr.PrivatePathCollisionError,
    tokens: Sequence[str] = (),
    allow: Sequence[str] = (),
) -> dict:
    reason = {
        "implementation_area": "private test suite",
        "observed_behavior": (
            "sealed private path collides with candidate: {0}".format(collision.path)
        ),
        "required_behavior": (
            "private tests must be hidden validator/meta-test files that "
            "exercise declared product outputs without replacing them"
        ),
        "violated_requirement": (
            "private tester paths must not collide with candidate files"
        ),
    }
    allowed = tuple(item for item in allow if item) + (
        collision.path,
        posixpath.basename(collision.path),
        collision.code,
        input_digest,
    )

    if tokens:
        blocked = tuple(token for token in tokens if token not in allowed)
        reason = {
            key: pr.redact_text(value, blocked) for key, value in reason.items()
        }
    payload = {
        "code": collision.code,
        "input_artifact_ids": list(input_artifact_ids),
        "input_digest": input_digest,
        "kind": st.ArtifactKind.TEST_INVALIDATION.value,
        "reason": reason,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
    }
    if tokens:
        pr.refuse_private_leak(payload, tokens, allow=allowed)
    return payload



def builder_view(
    *,
    public_contract: Mapping[str, object],
    architecture_constraints: Sequence[str],
    sealed_digest: str,
    prior_code_review: st.LaneArtifact | None = None,
    private_tokens: Sequence[str] = (),
) -> dict:
    contract = pr.public_contract(
        acceptance_criteria=pr.as_str_tuple(
            public_contract["acceptance_criteria"], "acceptance_criteria"
        ),
        declared_outputs=pr.as_str_tuple(
            public_contract["declared_outputs"], "declared_outputs"
        ),
    )
    if prior_code_review is None:
        prior: object = st.NO_CODE_REVIEW
    else:
        if prior_code_review.kind is not st.ArtifactKind.CODE_REVIEW:
            raise pr.PrivateReviewError("prior review must be CODE_REVIEW")
        if prior_code_review.verdict is not st.ReviewerVerdict.REVISE:
            raise pr.PrivateReviewError(
                "builder revise view requires CODE_REVIEW REVISE"
            )
        prior = {
            "findings": list(prior_code_review.payload["findings"]),
            "public_result_summary": dict(
                prior_code_review.payload["public_result_summary"]
            ),
            "verdict": st.ReviewerVerdict.REVISE.value,
        }
    view = {
        "architecture_constraints": [
            pr._nonempty(item, "architecture constraint")
            for item in architecture_constraints
        ],
        "declared_outputs": list(contract["declared_outputs"]),
        "prior_code_review": prior,
        "public_contract": contract,
        "sealed_digest": sealed_digest,
    }
    if not view["architecture_constraints"]:
        raise pr.PrivateReviewError("builder view requires architecture_constraints")
    tokens = tuple(private_tokens)
    if tokens:
        pr.refuse_private_leak(
            view,
            tokens,
            allow=pr.public_contract_allow(contract, extra=(sealed_digest,)),
        )
    return view


def review_builder_output(
    *,
    request: pr.VaultLaneRequest,
    state_root: Path,
    candidate_repo: Path,
    candidate_sha: str,
    candidate_ref: str,
    builder_base_sha: str,
    sealed_bundle: st.LaneArtifact,
    verdict: st.ReviewerVerdict,
    findings: Sequence[Mapping[str, str]] = (),
    scratch_root: Path,
    architecture_constraints: Sequence[str],
    allow_candidate_paths: bool = False,
    public_contract: Mapping[str, object] | None = None,
    gate: Mapping[str, object] | object | None = None,
    runtime_root: Path | None = None,
) -> st.LaneArtifact:
    if sealed_bundle.kind is not st.ArtifactKind.SEALED_TEST_BUNDLE:
        raise pr.PrivateReviewError("code review requires SEALED_TEST_BUNDLE")
    st.require_git_sha(candidate_sha, name="candidate_sha")
    st.require_git_sha(builder_base_sha, name="builder_base_sha")
    if not candidate_ref.startswith("refs/maestro/candidates/"):
        raise pr.PrivateReviewError(
            "candidate_ref must be an immutable maestro candidate"
        )
    vault = hv.ensure_vault(state_root, request.run_id)
    files = tc.sealed_private_files(vault, sealed_bundle)
    if not files:
        raise pr.PrivateReviewError("sealed bundle has no private tests")
    dest = Path(scratch_root) / "review-{0}-{1}".format(
        request.lane_id, request.input_digest[:12]
    )
    _review_tree(candidate_repo, candidate_sha, dest)
    if not allow_candidate_paths:
        _refuse_candidate_private_collisions(dest, files)
    hv.copy_blobs_to_tree(vault, dest, files)
    run = tc.run_private_suite(
        dest,
        tuple(files),
        gate=gate,
        runtime_root=runtime_root or candidate_repo,
    )


    summary = {
        "errored": run["counts"]["errored"],
        "executed": run["executed"],
        "failed": run["counts"]["failed"],
        "passed": run["counts"]["passed"],
        "skipped": run["counts"]["skipped"],
    }
    min_cases = int(run.get("min_cases") or 1)
    runner_failed = bool(
        run["returncode"] != 0
        or summary["failed"]
        or summary["errored"]
        or summary["executed"] < min_cases
    )
    if runner_failed:
        if verdict is st.ReviewerVerdict.PASS:
            verdict = st.ReviewerVerdict.REVISE
        if not findings:
            findings = (_RUNNER_REVISE,)
    private_files = {
        path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in files.items()
    }
    results_ref = hv.private_results_ref(
        request.run_id, request.lane_id, request.input_digest
    )
    tokens = pr.collect_private_tokens(
        files=private_files,
        vault_path=vault,
        vault_refs=(sealed_bundle.artifact_ref, results_ref),
        blob_ids=tuple(files.values()),
    )
    findings_out = pr.actionable_findings(verdict, findings, tokens)
    private_bytes = st.canonical_bytes(
        {
            "counts": run["counts"],
            "executed": run["executed"],
            "output": run["output"],
            "returncode": run["returncode"],
        }
    )
    results_digest = st.digest_bytes(private_bytes)
    results_blob = hv.hash_blob(vault, private_bytes)
    hv.pin_object_ref(vault, results_ref, results_blob)
    sealed_digest = sealed_bundle.payload["sealed_digest"]
    payload = {
        "builder_base_sha": builder_base_sha,
        "candidate_ref": candidate_ref,
        "candidate_sha": candidate_sha,
        "findings": [dict(item) for item in findings_out],
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "private_results_digest": results_digest,
        "public_result_summary": summary,
        "sealed_digest": sealed_digest,
        "verdict": verdict.value,
    }
    allowed = (
        request.input_digest,
        sealed_digest,
        results_digest,
        candidate_sha,
        builder_base_sha,
        candidate_ref,
    )
    pr.refuse_private_leak(payload, tokens, allow=allowed)
    artifact = pr.make_lane_artifact(
        kind=st.ArtifactKind.CODE_REVIEW,
        request=request,
        payload=payload,
        artifact_ref=results_ref,
        verdict=verdict,
    )
    pr.refuse_private_leak(st.canonical_bytes(artifact.payload), tokens, allow=allowed)
    view = builder_view(
        public_contract=public_contract or sealed_bundle.payload["public_contract"],
        architecture_constraints=architecture_constraints,
        sealed_digest=sealed_digest,
        prior_code_review=artifact if verdict is st.ReviewerVerdict.REVISE else None,
        private_tokens=tokens,
    )
    if (
        verdict is st.ReviewerVerdict.REVISE
        and view["prior_code_review"] == st.NO_CODE_REVIEW
    ):
        raise pr.PrivateReviewError("REVISE builder view lost the prior review")
    return artifact
