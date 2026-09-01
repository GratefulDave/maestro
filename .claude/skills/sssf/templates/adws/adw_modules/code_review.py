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
from .launcher import PROVISION_TIMEOUT_S, run_harness_process

_RUNNER_REVISE = {
    "implementation_area": "declared product outputs",
    "observed_behavior": "sealed private tests failed, errored, or did not execute",
    "required_behavior": "the candidate must pass every sealed private test",
    "violated_requirement": "accepted sealed tests bind the candidate",
}

#: A candidate that breaks collection IS a candidate defect -- an import it
#: broke, a module it deleted -- and gets a normal REVISE, distinct from the
#: "tests failed" wording so the builder is told what actually happened.
_COLLECTION_REVISE = {
    "implementation_area": "declared product outputs",
    "observed_behavior": (
        "the sealed suite collected no case against the candidate, though it "
        "collects against the commit the candidate was built from"
    ),
    "required_behavior": (
        "the candidate must remain importable so the sealed suite can collect"
    ),
    "violated_requirement": "accepted sealed tests bind the candidate",
}

_INTEGRATION_GATE_REVISE = {
    "implementation_area": "merged integration surface",
    "observed_behavior": "a run-level sealed suite failed against the merged integration",
    "required_behavior": "the merged integration must pass every run-level sealed suite",
    "violated_requirement": "an unconsumed tests lane gates the run, not a single lane",
}


def run_integration_gate(
    *,
    run_id: str,
    lane_id: str,
    input_digest: str,
    state_root: Path,
    integration_repo: Path,
    integration_sha: str,
    sealed_bundle: st.LaneArtifact,
    scratch_root: Path,
    gate: Mapping[str, object] | object | None = None,
    runtime_root: Path | None = None,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
) -> Mapping[str, object]:
    """Run one unconsumed lane's sealed suite against the merged integration head.

    Same primitives as review_builder_output, aimed at the integration head instead
    of a single lane's candidate. Read-only with respect to the vault and the repo.
    """
    if sealed_bundle.kind is not st.ArtifactKind.SEALED_TEST_BUNDLE:
        raise pr.PrivateReviewError("integration gate requires SEALED_TEST_BUNDLE")
    st.require_git_sha(integration_sha, name="integration_sha")
    vault = hv.ensure_vault(state_root, run_id)
    files = tc.sealed_private_files(vault, sealed_bundle)
    if not files:
        raise pr.PrivateReviewError("sealed bundle has no private tests")
    dest = Path(scratch_root) / "integration-gate-{0}-{1}".format(
        lane_id, input_digest[:12]
    )
    _review_tree(
        integration_repo, integration_sha, dest, provision_argv, provision_timeout_s
    )
    hv.copy_blobs_to_tree(vault, dest, files)
    run = tc.run_private_suite(
        dest,
        tuple(files),
        gate=gate,
        runtime_root=runtime_root or integration_repo,
    )
    min_cases = int(run.get("min_cases") or 1)
    counts = run["counts"]
    failed = bool(
        run["returncode"] != 0
        or counts["failed"]
        or counts["errored"]
        or run["executed"] < min_cases
    )
    return {
        "counts": counts,
        "executed": run["executed"],
        "failed": failed,
        "lane_id": lane_id,
        "min_cases": min_cases,
    }


#: The operator-facing outcome for a harness environment fault. Deliberately
#: distinct from every candidate-defect surface and from the plan / runtime /
#: repository-binding refusals, which name a decision the operator can revise.
#: This one names a machine they have to fix.
SEALED_ENVIRONMENT_OUTCOME = pr.SealedEnvironmentError.code

#: Appended to every environment detail. The operator's first question about a
#: refused review is whose fault it is, and the answer is never the builder's.
NEVER_EXECUTED = (
    "the sealed suite never executed, so this is not a candidate defect and "
    "the builder cannot fix it; repair the review environment and resume"
)


#: The shared base for a harness environment fault, owned by `private_review`
#: because the import direction is code_review -> tests_chain -> private_review:
#: a base class here would be a cycle for the runner chain. Re-exported so this
#: module's callers catch it by this name.
SealedEnvironmentError = pr.SealedEnvironmentError


def sealed_environment_detail(exc: BaseException) -> str | None:
    """One operator-facing detail line, or None if `exc` is not environmental.

    Recognition is by class alone. It was briefly a match on the code a message
    opens with, which was wrong for the reason that showed up within the hour: a
    renamed code, or a fifth and sixth case, silently stops being recognised and
    the operator is back to a traceback.

    Preserves the raiser's own message verbatim -- resolved invocation, measured
    version, the specifier, the declaring file -- because that is what tells the
    operator which interpreter to install, and appends who is not at fault.
    """
    if not isinstance(exc, pr.SealedEnvironmentError):
        return None
    return "{0} | {1}".format(exc, NEVER_EXECUTED)


class SealedSuiteNotCollectedError(SealedEnvironmentError):
    """The sealed suite produced no case outcome, and did not at the base either.

    "The suite could not be collected" and "the suite ran and failed" are facts
    about different actors, and only the second can be the candidate's. A
    collection failure that is already present at the commit the builder started
    from cannot have been caused by the builder -- an undeclared dependency, a
    conftest importing something the manifests never named -- so it is an
    environment fault and is refused rather than recorded against them.
    """

    code = "SEALED_SUITE_NOT_COLLECTED"

    def __init__(self, returncode: int, detail: str = "") -> None:
        self.returncode = returncode
        self.detail = detail
        super().__init__(
            "{0}:{1}:{2}".format(self.code, returncode, detail)
        )


def _collected_no_case(run: Mapping[str, object]) -> bool:
    """Whether the runner reported no case outcome of any kind.

    Not "fewer cases than `min_cases`" and not "the suite failed": zero passed,
    zero failed, zero errored, zero skipped. No runner reports that for a suite
    that actually ran, so it means collection never produced a case.
    """
    counts = run["counts"]
    return int(run["executed"]) == 0 and not any(
        int(counts[key]) for key in ("passed", "failed", "errored", "skipped")
    )


class ReviewProvisioningError(SealedEnvironmentError):
    """The review tree could not be provisioned, so no verdict is derivable.

    Typed and fail-closed on purpose. A provisioning failure is a failure of the
    review harness, never of the candidate: an unprovisioned tree collects zero
    cases, and `_RUNNER_REVISE` would then report that as "sealed private tests
    failed" and send the builder to fix tests that never ran. Raising instead of
    returning a verdict makes that mislabelling impossible -- no CODE_REVIEW
    artifact is built on this path at all.
    """

    code = "REVIEW_TREE_PROVISION_FAILED"

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int | None,
        detail: str = "",
    ) -> None:
        self.argv = tuple(str(item) for item in argv)
        self.returncode = returncode
        self.detail = detail
        super().__init__(
            "{0}:{1}:{2}:{3}".format(
                self.code, " ".join(self.argv), returncode, detail
            )
        )


def _provision_review_tree(
    dest: Path,
    provision_argv: Sequence[str],
    timeout_s: float | None = None,
) -> None:
    """Install the candidate's declared dependencies into the review tree.

    Ordering is a containment property, not a convenience: this runs after the
    commit is materialized and before any sealed blob is copied in, so nothing
    provisioning writes, reads, or reports back in an error can carry private
    test bytes.

    It also runs once per materialization by construction.
    `hv.refresh_materialized_commit` unlinks every child of the tree, which is
    every installed dependency and any marker a previous run could have left, so
    there is no "already provisioned" state inside the tree to detect. The
    durable cache is the package manager's own, outside the tree.
    """
    argv = tuple(str(item) for item in provision_argv if str(item))
    if not argv:
        return
    bound = PROVISION_TIMEOUT_S if timeout_s is None else float(timeout_s)
    try:
        result = run_harness_process(argv, cwd=Path(dest), timeout=bound)
    except OSError as exc:
        # TimeoutError is an OSError; a missing provisioning executable is one
        # too. Both are the harness failing, and both must stay distinguishable
        # from a candidate that failed its tests.
        raise ReviewProvisioningError(argv, None, str(exc)) from exc
    if result.returncode != 0:
        raise ReviewProvisioningError(
            argv, result.returncode, (result.stderr or "")[-400:]
        )


def _review_tree(
    repo: Path,
    sha: str,
    dest: Path,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
) -> Path:
    dest = Path(dest)
    if dest.exists() and dest.is_dir() and any(dest.iterdir()):
        tree = hv.refresh_materialized_commit(repo, sha, dest)
    else:
        tree = hv.materialize_commit(repo, sha, dest)
    _provision_review_tree(tree, provision_argv, provision_timeout_s)
    return tree



_NO_RUN = {
    "counts": {"passed": 0, "failed": 0, "errored": 0, "skipped": 0},
    "executed": 0,
    "min_cases": 1,
    "output": "",
    "returncode": -1,
    "runner": "",
}


def _run_sealed_suite(
    tree: Path,
    files: Mapping[str, str],
    *,
    gate: Mapping[str, object] | object | None,
    runtime_root: Path,
) -> tuple[Mapping[str, object], pr.PrivateReviewError | None]:
    """Run the sealed suite, treating an environment refusal as a measurement.

    A suite whose imports fail does not reach a case count at all: the runner
    probe collects the tree, exits 2 rather than the capable 5, and
    `run_private_suite` raises `SEALED_SUITE_RUNNER_UNUSABLE`. That refusal and
    a zero-case run are the same observation -- "no case outcome exists" -- and
    both are ambiguous about who caused it, so both are returned rather than
    thrown. Every other `PrivateReviewError` names a factory invariant and
    propagates untouched.
    """
    try:
        return tc.run_private_suite(
            tree, tuple(files), gate=gate, runtime_root=runtime_root
        ), None
    except pr.PrivateReviewError as exc:
        if sealed_environment_detail(exc) is None:
            raise
        run = {key: value for key, value in _NO_RUN.items()}
        run["counts"] = dict(_NO_RUN["counts"])
        run["output"] = str(exc)
        return run, exc


def _collect_at_base(
    *,
    candidate_repo: Path,
    builder_base_sha: str,
    vault: Path,
    files: Mapping[str, str],
    scratch_root: Path,
    lane_id: str,
    input_digest: str,
    gate: Mapping[str, object] | object | None,
    runtime_root: Path,
    provision_argv: Sequence[str],
    provision_timeout_s: float | None,
) -> bool:
    """Whether the same sealed suite reaches a case outcome at the base commit.

    Its own tree, so the candidate's review tree is never mutated by the
    measurement. False when the base cannot be provisioned, cannot resolve a
    runner, or collects nothing -- all of which mean the fault predates the
    candidate and cannot have been caused by the builder.
    """
    base_dest = Path(scratch_root) / "review-base-{0}-{1}".format(
        lane_id, input_digest[:12]
    )
    try:
        _review_tree(
            candidate_repo,
            builder_base_sha,
            base_dest,
            provision_argv,
            provision_timeout_s,
        )
        hv.copy_blobs_to_tree(vault, base_dest, files)
        run, refusal = _run_sealed_suite(
            base_dest, files, gate=gate, runtime_root=runtime_root
        )
    except (pr.PrivateReviewError, hv.VaultError, OSError):
        return False
    return refusal is None and not _collected_no_case(run)


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
    # Deliberately unprovisioned: this executes no suite, only a path check, and
    # `review_builder_output` re-materializes this same dest straight afterwards,
    # which would unlink anything installed here.
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
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
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
    _review_tree(
        candidate_repo, candidate_sha, dest, provision_argv, provision_timeout_s
    )
    if not allow_candidate_paths:
        _refuse_candidate_private_collisions(dest, files)
    hv.copy_blobs_to_tree(vault, dest, files)
    run, refusal = _run_sealed_suite(
        dest, files, gate=gate, runtime_root=runtime_root or candidate_repo
    )
    if refusal is not None or _collected_no_case(run):
        # "No case outcome" is ambiguous on its own: an undeclared dependency
        # and a candidate that broke an import both produce it, and under the
        # runner probe both arrive as the same refusal. The commit the builder
        # started from settles it -- a suite that reaches no outcome there
        # either was already broken before the candidate existed. Paid only on
        # this path, which today costs the builder a full revise round.
        if not _collect_at_base(
            candidate_repo=candidate_repo,
            builder_base_sha=builder_base_sha,
            vault=vault,
            files=files,
            scratch_root=scratch_root,
            lane_id=request.lane_id,
            input_digest=request.input_digest,
            gate=gate,
            runtime_root=runtime_root or candidate_repo,
            provision_argv=provision_argv,
            provision_timeout_s=provision_timeout_s,
        ):
            # Not the candidate's doing. Re-raise the runner's own refusal when
            # there was one, so its interpreter detail reaches the operator.
            if refusal is not None:
                raise refusal
            raise SealedSuiteNotCollectedError(
                int(run["returncode"]),
                "the sealed suite reaches no case outcome at the candidate and "
                "none at its base {0}, so the candidate did not cause it".format(
                    builder_base_sha[:12]
                ),
            )
        # The base reaches outcomes and the candidate does not, so the candidate
        # broke collection. That is a defect, and it is reviewed as one.
        if not findings:
            findings = (_COLLECTION_REVISE,)

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
