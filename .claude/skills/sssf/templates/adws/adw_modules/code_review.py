"""Builder view and CODE_REVIEW LaneArtifact. Never writes lane_state.

The builder receives the public contract, architecture constraints, allowed
paths, the paths of the accepted suite it is graded against, that suite's
digest, and the prior review with the runner's failure output verbatim. The
suite itself is in the builder's checkout; what the builder may not do is
write to it, and a candidate that does is refused before any reviewer reads
it (`CANDIDATE_TEST_PATH_REFUSED`). Every site that runs the suite verifies
the tree against the accepted blobs first (`TEST_SUITE_TAMPERED`), so the
measurement is always of the accepted cases and never of a rewrite.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Mapping, Sequence

from . import provisioning as prov
from . import review_contract as rc
from . import runner_resolution as rr
from . import scheduler_types as st
from . import test_binding as tb
from . import tests_chain as tc
from . import tree_materialize as tm

_RUNNER_REVISE = {
    "implementation_area": "declared product outputs",
    "observed_behavior": "accepted tests failed, errored, or did not execute",
    "required_behavior": "the candidate must pass every accepted test",
    "violated_requirement": "accepted tests bind the candidate",
}

#: A candidate that breaks collection IS a candidate defect -- an import it
#: broke, a module it deleted -- and gets a normal REVISE, distinct from the
#: "tests failed" wording so the builder is told what actually happened.
_COLLECTION_REVISE = {
    "implementation_area": "declared product outputs",
    "observed_behavior": (
        "the accepted suite collected no case against the candidate, though it "
        "collects against the commit the candidate was built from"
    ),
    "required_behavior": (
        "the candidate must remain importable so the accepted suite can collect"
    ),
    "violated_requirement": "accepted tests bind the candidate",
}

_INTEGRATION_GATE_REVISE = {
    "implementation_area": "merged integration surface",
    "observed_behavior": "a run-level accepted suite failed against the merged integration",
    "required_behavior": "the merged integration must pass every run-level accepted suite",
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
    accepted_suite: st.LaneArtifact,
    scratch_root: Path,
    gate: Mapping[str, object] | object | None = None,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
) -> Mapping[str, object]:
    """Run one unconsumed lane's accepted suite against the merged integration head.

    Same primitives as review_builder_output, aimed at the integration head instead
    of a single lane's candidate. The suite is in the integration tree -- the
    tests lane merged it -- so the tree is materialized and verified, not overlaid.
    """
    del run_id
    if accepted_suite.kind is not st.ArtifactKind.ACCEPTED_TEST_SUITE:
        raise rc.ReviewContractError("integration gate requires ACCEPTED_TEST_SUITE")
    st.require_git_sha(integration_sha, name="integration_sha")
    files = tc.suite_files(accepted_suite)
    dest = Path(scratch_root) / "integration-gate-{0}-{1}".format(
        lane_id, input_digest[:12]
    )
    _review_tree(
        integration_repo,
        integration_sha,
        dest,
        provision_argv,
        provision_timeout_s,
        state_root=state_root,
    )
    # `failed` here is read by `FactoryScheduler._failed_run_gates`, which
    # stands between a merged surface and both the final review and
    # publication. A gate that measured bytes nobody accepted is a
    # publication decision made on a forgery, so the tree is verified first.
    tb.verify_suite(files, dest)
    run = tc.run_suite(dest, tuple(files), gate=gate)
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
        "test_suite_digest": tb.suite_digest(files),
    }


#: The operator-facing outcome for a harness environment fault. Deliberately
#: distinct from every candidate-defect surface and from the plan / runtime /
#: repository-binding refusals, which name a decision the operator can revise.
#: This one names a machine they have to fix.
SUITE_ENVIRONMENT_OUTCOME = rc.SuiteEnvironmentError.code

#: Appended to every environment detail. The operator's first question about a
#: refused review is whose fault it is, and the answer is never the builder's.
NEVER_EXECUTED = (
    "the suite never executed, so this is not a candidate defect and "
    "the builder cannot fix it; repair the review environment and resume"
)


#: The shared base for a harness environment fault, owned by `review_contract`
#: because the import direction is code_review -> tests_chain -> review_contract:
#: a base class here would be a cycle for the runner chain. Re-exported so this
#: module's callers catch it by this name.
SuiteEnvironmentError = rc.SuiteEnvironmentError


def suite_environment_detail(exc: BaseException) -> str | None:
    """One operator-facing detail line, or None if `exc` is not environmental.

    Recognition is by class alone. It was briefly a match on the code a message
    opens with, which was wrong for the reason that showed up within the hour: a
    renamed code, or a fifth and sixth case, silently stops being recognised and
    the operator is back to a traceback.

    Preserves the raiser's own message verbatim -- resolved invocation, measured
    version, the specifier, the declaring file -- because that is what tells the
    operator which interpreter to install, and appends who is not at fault.
    """
    if not isinstance(exc, rc.SuiteEnvironmentError):
        return None
    return "{0} | {1}".format(exc, NEVER_EXECUTED)


class SuiteNotCollectedError(SuiteEnvironmentError):
    """The suite produced no case outcome, and did not at the base either.

    "The suite could not be collected" and "the suite ran and failed" are facts
    about different actors, and only the second can be the candidate's. A
    collection failure that is already present at the commit the builder started
    from cannot have been caused by the builder -- an undeclared dependency, a
    conftest importing something the manifests never named -- so it is an
    environment fault and is refused rather than recorded against them.
    """

    code = "SUITE_NOT_COLLECTED"

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


# `ReviewProvisioningError` and `provision_tree` live in `provisioning`, the one
# provisioner every factory tree crosses. Re-exported here for existing imports;
# the call below goes through the module so one patch of `prov.provision_tree`
# observes every site.
ReviewProvisioningError = prov.ReviewProvisioningError
provision_tree = prov.provision_tree


def _review_tree(
    repo: Path,
    sha: str,
    dest: Path,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
    *,
    state_root: Path,
) -> Path:
    dest = Path(dest)
    if dest.exists() and dest.is_dir() and any(dest.iterdir()):
        tree = tm.refresh_materialized_commit(repo, sha, dest, state_root=state_root)
    else:
        tree = tm.materialize_commit(repo, sha, dest, state_root=state_root)
    prov.provision_tree(tree, provision_argv, provision_timeout_s)
    return tree


_NO_RUN = {
    "counts": {"passed": 0, "failed": 0, "errored": 0, "skipped": 0},
    "executed": 0,
    "min_cases": 1,
    "output": "",
    "returncode": -1,
    "runner": "",
}


def _run_suite(
    tree: Path,
    files: Mapping[str, str],
    *,
    gate: Mapping[str, object] | object | None,
) -> tuple[Mapping[str, object], rc.ReviewContractError | None]:
    """Run the suite, treating an environment refusal as a measurement.

    A suite whose imports fail does not reach a case count at all: the runner
    probe collects the tree, exits 2 rather than the capable 5, and
    `run_suite` raises `SUITE_RUNNER_UNUSABLE`. That refusal and a zero-case
    run are the same observation -- "no case outcome exists" -- and both are
    ambiguous about who caused it, so both are returned rather than thrown.
    Every other `ReviewContractError` names a factory invariant and propagates
    untouched.
    """
    try:
        return tc.run_suite(tree, tuple(files), gate=gate), None
    except rc.ReviewContractError as exc:
        if suite_environment_detail(exc) is None:
            raise
        run = {key: value for key, value in _NO_RUN.items()}
        run["counts"] = dict(_NO_RUN["counts"])
        run["output"] = str(exc)
        return run, exc


def _collect_at_base(
    *,
    candidate_repo: Path,
    builder_base_sha: str,
    files: Mapping[str, str],
    scratch_root: Path,
    lane_id: str,
    input_digest: str,
    gate: Mapping[str, object] | object | None,
    provision_argv: Sequence[str],
    provision_timeout_s: float | None,
    state_root: Path,
) -> bool:
    """Whether the same accepted suite reaches a case outcome at the base commit.

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
            state_root=state_root,
        )
        tb.verify_suite(files, base_dest)
        run, refusal = _run_suite(base_dest, files, gate=gate)
    except (tm.TreeContainmentRefused, tb.TestSuiteTampered):
        # Neither is a fact about the base commit, and both must escape the
        # absolution below. Where the tree may be written is one. The other is
        # the suite itself not being the accepted suite -- a
        # `ReviewContractError` by inheritance, so the broad clause would read
        # it as "the fault predates the candidate", return False, and let the
        # candidate be measured against bytes nobody accepted while reporting
        # the builder blameless. An operator fault absorbed as an absolution is
        # worse than one that stops the run.
        raise
    except (rc.ReviewContractError, tm.MaterializeError, OSError):
        return False
    return refusal is None and not _collected_no_case(run)


# Lines a runner uses to say what went wrong. pytest prefixes assertion output
# with "E ", vitest marks a failing case with a cross and names the error class
# inline.
_FAILURE_MARKERS = (
    "AssertionError",
    "Error:",
    "TypeError",
    "ValueError",
    "expected ",
    "is not a function",
)

#: Shapes that name a failing case: pytest's short summary and `-v` outcome
#: lines, vitest's FAIL header and its file pointer. Kept, because the case a
#: failure belongs to is the first thing the builder needs to open the suite
#: at the right assertion.
_FAILED_CASE_PREFIXES = ("FAILED", "FAIL ", "ERROR", "✕", "×", "❯")


def _names_a_failed_case(line: str) -> bool:
    if line.startswith(_FAILED_CASE_PREFIXES):
        return True
    return "::" in line and (" FAILED" in line or " ERROR" in line)


# Enough to name every failure in a normal suite without turning the builder
# prompt into a log file.
_MAX_FAILURE_LINES = 40
_MAX_FAILURE_LINE_CHARS = 300

#: A comparison body under a kept failure line: pytest's `Left contains N more
#: items:` block and its dict rendering, vitest's diff. These name what
#: differs, which is the whole answer to a shape mismatch.
_CONTINUATION_PREFIXES = ("+", "-", "{", "}", "'", '"', "E ", "Left ", "Right ", "Omitting", "Full output")


def _is_continuation(raw: str) -> bool:
    if not raw.strip():
        return False
    if raw[:1].isspace():
        return True
    return raw.lstrip().startswith(_CONTINUATION_PREFIXES)


def failure_lines(
    output: str,
    *,
    limit: int = _MAX_FAILURE_LINES,
) -> tuple[str, ...]:
    """The runner's own failure lines, verbatim.

    The builder is otherwise told only how many cases failed, never which ones,
    so it re-guesses the same fix every round. These lines are what it is
    missing: the case, the error class, the assertion, and the comparison body
    under it. Nothing is redacted -- the suite is in the builder's checkout --
    and nothing is summarised: a builder that reads `assert x == 3` fixes the
    right thing on turn one, and a builder that reads `[redacted]` plateaued
    for rounds on ambiguities the assertion would have settled.
    """
    seen: set[str] = set()
    kept: list[str] = []
    in_block = False
    for raw in output.splitlines():
        # vitest colours its diff, so `- Expected` arrives as `\\x1b[32m- Expected
        # \\x1b[39m`; strip once so the marker test, the continuation test and
        # the kept text all read the same line. The expression is
        # `runner_resolution`'s, not a second copy.
        raw = rr._ANSI.sub("", raw)
        line = raw.strip()
        if not line:
            # A blank line separates a comparison body from its header, it does
            # not end it: vitest emits a truly empty line either side of
            # `- Expected / + Received`.
            continue
        named = _names_a_failed_case(line)
        marked = line.startswith("E ") or any(
            marker in line for marker in _FAILURE_MARKERS
        )
        continued = in_block and _is_continuation(raw)
        if not named and not marked and not continued:
            in_block = False
            continue
        in_block = True
        clean = line[:_MAX_FAILURE_LINE_CHARS]
        if clean in seen:
            continue
        seen.add(clean)
        kept.append(clean)
        if len(kept) >= limit:
            break
    return tuple(kept)


#: One line of framing, prepended to the findings the builder is handed when
#: the suite was red. The failure lines come off the runner; the findings are
#: a model's reading of a candidate, and twice in one session that reading was
#: the exact inverse of the assertion -- a builder told to preserve the very
#: keys a case forbids regressed a lane that had been green. Nothing is
#: deleted here: a located finding such as "you wrote outside your declared
#: outputs" is true whatever the suite says. Only the order of authority is
#: stated.
_FINDINGS_FRAMING = (
    "The accepted suite reported failures against your last candidate. Its "
    "failure lines in failure_output are ground truth, verbatim from the "
    "runner, and the suite itself is in your checkout at the paths named in "
    "test_paths. The findings below are one model's reading of them. Where a "
    "finding disagrees with a failure line or with the suite, the suite is "
    "right."
)


def _summary_is_red(summary: Mapping[str, object]) -> bool:
    """Whether the measured suite disagreed with the candidate.

    Counts arrive off an artifact payload, so a missing or unparsable key is
    read as zero rather than raised on: framing that fails closed would turn
    a malformed count into a lost review.
    """
    for key in ("failed", "errored"):
        try:
            if int(summary.get(key, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def builder_view(
    *,
    public_contract: Mapping[str, object],
    architecture_constraints: Sequence[str],
    test_suite_digest: str,
    test_paths: Sequence[str] = (),
    prior_code_review: st.LaneArtifact | None = None,
) -> dict:
    contract = rc.public_contract(
        acceptance_criteria=rc.as_str_tuple(
            public_contract["acceptance_criteria"], "acceptance_criteria"
        ),
        declared_outputs=rc.as_str_tuple(
            public_contract["declared_outputs"], "declared_outputs"
        ),
    )
    if prior_code_review is None:
        prior: object = st.NO_CODE_REVIEW
    else:
        if prior_code_review.kind is not st.ArtifactKind.CODE_REVIEW:
            raise rc.ReviewContractError("prior review must be CODE_REVIEW")
        if prior_code_review.verdict is not st.ReviewerVerdict.REVISE:
            raise rc.ReviewContractError(
                "builder revise view requires CODE_REVIEW REVISE"
            )
        summary = dict(prior_code_review.payload["public_result_summary"])
        findings = list(prior_code_review.payload["findings"])
        if _summary_is_red(summary):
            findings.insert(0, _FINDINGS_FRAMING)
        prior = {
            "failure_output": list(
                prior_code_review.payload.get("failure_output") or ()
            ),
            "findings": findings,
            "public_result_summary": summary,
            "verdict": st.ReviewerVerdict.REVISE.value,
        }
    view = {
        "architecture_constraints": [
            rc._nonempty(item, "architecture constraint")
            for item in architecture_constraints
        ],
        "declared_outputs": list(contract["declared_outputs"]),
        "prior_code_review": prior,
        "public_contract": contract,
        "test_paths": sorted(str(path) for path in test_paths),
        "test_suite_digest": test_suite_digest,
    }
    if not view["architecture_constraints"]:
        raise rc.ReviewContractError("builder view requires architecture_constraints")
    return view


@dataclasses.dataclass(frozen=True)
class SuiteMeasurement:
    """What the accepted suite says about one candidate.

    Measured BEFORE the reviewer agent votes, so the agent can be told that
    the suite is red and how badly. The counts ship to the builder as
    `public_result_summary`; the runner's failure lines ship as
    `failure_output`.
    """

    summary: Mapping[str, int]
    runner_failed: bool
    collection_broken: bool
    min_cases: int
    run: Mapping[str, object]
    files: Mapping[str, str]


def measure_candidate(
    *,
    request: rc.LaneRequest,
    state_root: Path,
    candidate_repo: Path,
    candidate_sha: str,
    candidate_ref: str,
    builder_base_sha: str,
    accepted_suite: st.LaneArtifact,
    scratch_root: Path,
    gate: Mapping[str, object] | object | None = None,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
) -> SuiteMeasurement:
    """Run the accepted suite against a candidate and report counts and output.

    Split out of `review_builder_output` so the scheduler can measure first
    and hand the reviewer agent the result before it votes. A reviewer told
    "5 of 12 cases fail" reads the diff for a cause; a reviewer told nothing
    returns PASS and the builder learns only that something broke.

    The review tree is the materialized candidate and nothing else. The suite
    is in it because the candidate descends from a base that carries it, and
    `verify_suite` is what proves the bytes under those paths are the accepted
    ones: a mismatch is `TEST_SUITE_TAMPERED`, an environment refusal that
    reaches the operator instead of billing the builder a round for it.
    """
    if accepted_suite.kind is not st.ArtifactKind.ACCEPTED_TEST_SUITE:
        raise rc.ReviewContractError("code review requires ACCEPTED_TEST_SUITE")
    st.require_git_sha(candidate_sha, name="candidate_sha")
    st.require_git_sha(builder_base_sha, name="builder_base_sha")
    if not candidate_ref.startswith("refs/maestro/candidates/"):
        raise rc.ReviewContractError(
            "candidate_ref must be an immutable maestro candidate"
        )
    files = tc.suite_files(accepted_suite)
    dest = Path(scratch_root) / "review-{0}-{1}".format(
        request.lane_id, request.input_digest[:12]
    )
    _review_tree(
        candidate_repo,
        candidate_sha,
        dest,
        provision_argv,
        provision_timeout_s,
        state_root=state_root,
    )
    tb.verify_suite(files, dest)
    run, refusal = _run_suite(dest, files, gate=gate)
    collection_broken = False
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
            files=files,
            scratch_root=scratch_root,
            lane_id=request.lane_id,
            input_digest=request.input_digest,
            gate=gate,
            provision_argv=provision_argv,
            provision_timeout_s=provision_timeout_s,
            state_root=state_root,
        ):
            # Not the candidate's doing. Re-raise the runner's own refusal when
            # there was one, so its interpreter detail reaches the operator.
            if refusal is not None:
                raise refusal
            raise SuiteNotCollectedError(
                int(run["returncode"]),
                "the suite reaches no case outcome at the candidate and "
                "none at its base {0}, so the candidate did not cause it".format(
                    builder_base_sha[:12]
                ),
            )
        # The base reaches outcomes and the candidate does not, so the candidate
        # broke collection. That is a defect, and it is reviewed as one.
        collection_broken = True

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
    return SuiteMeasurement(
        summary=summary,
        runner_failed=runner_failed,
        collection_broken=collection_broken,
        min_cases=min_cases,
        run=run,
        files=files,
    )


def review_builder_output(
    *,
    request: rc.LaneRequest,
    state_root: Path,
    candidate_repo: Path,
    candidate_sha: str,
    candidate_ref: str,
    builder_base_sha: str,
    accepted_suite: st.LaneArtifact,
    verdict: st.ReviewerVerdict,
    findings: Sequence[Mapping[str, str]] = (),
    scratch_root: Path,
    architecture_constraints: Sequence[str],
    public_contract: Mapping[str, object] | None = None,
    gate: Mapping[str, object] | object | None = None,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
    measurement: SuiteMeasurement | None = None,
) -> st.LaneArtifact:
    """Bind a reviewer verdict to the measurement of one candidate.

    `measurement` is the suite result when the caller already ran it -- the
    scheduler does, so the reviewer could see the counts before voting. When
    it is absent the suite runs here, which is what every direct caller and
    the older tests do. It is never run twice: provisioning a review tree
    costs minutes.
    """
    if measurement is None:
        measurement = measure_candidate(
            request=request,
            state_root=state_root,
            candidate_repo=candidate_repo,
            candidate_sha=candidate_sha,
            candidate_ref=candidate_ref,
            builder_base_sha=builder_base_sha,
            accepted_suite=accepted_suite,
            scratch_root=scratch_root,
            gate=gate,
            provision_argv=provision_argv,
            provision_timeout_s=provision_timeout_s,
        )
    files = measurement.files
    run = measurement.run
    summary = measurement.summary
    # The two axes are separated before anything reads a verdict off them. A
    # standards finding is a hygiene judgement call: it is capped at WARNING
    # and it is not in the set that decides whether this lane goes back to
    # BUILDING. Nothing below ranks one axis against the other -- they simply
    # never meet again.
    findings, _standards = st.partition_findings_by_axis(findings)
    if measurement.runner_failed:
        if verdict is st.ReviewerVerdict.PASS:
            verdict = st.ReviewerVerdict.REVISE
        if not findings:
            # The reviewer saw the counts and still said nothing locatable.
            # Say what happened rather than nothing; the scheduler has
            # already asked it a second time by the time this fires.
            findings = (
                _COLLECTION_REVISE if measurement.collection_broken else _RUNNER_REVISE,
            )
    results_digest = st.digest_bytes(
        st.canonical_bytes(
            {
                "counts": run["counts"],
                "executed": run["executed"],
                "output": run["output"],
                "returncode": run["returncode"],
            }
        )
    )
    findings_out = rc.actionable_findings(verdict, findings)
    test_suite_digest = str(accepted_suite.payload["test_suite_digest"])
    payload = {
        "builder_base_sha": builder_base_sha,
        "candidate_ref": candidate_ref,
        "candidate_sha": candidate_sha,
        "failure_output": list(failure_lines(str(run["output"]))),
        "findings": [dict(item) for item in findings_out],
        "input_artifact_ids": list(request.input_artifact_ids),
        "input_digest": request.input_digest,
        "public_result_summary": summary,
        "results_digest": results_digest,
        # Recomputable from the tree the suite ran in, which is what makes
        # "the review ran the accepted suite" checkable after the fact.
        "test_suite_digest": tb.suite_digest(files),
        "verdict": verdict.value,
    }
    if payload["test_suite_digest"] != test_suite_digest:
        raise rc.ReviewContractError("measured suite is not the accepted suite")
    # The ref is keyed on the digest of the runner's own result bytes, not on
    # the review's input: the runner's stdout is an observation -- its summary
    # line carries a wall-clock duration -- so two reviews of one input
    # legitimately produce two results, and each names its own artifact.
    artifact = rc.make_lane_artifact(
        kind=st.ArtifactKind.CODE_REVIEW,
        request=request,
        payload=payload,
        artifact_ref="code-review:{0}".format(results_digest),
        verdict=verdict,
    )
    view = builder_view(
        public_contract=public_contract or accepted_suite.payload["public_contract"],
        architecture_constraints=architecture_constraints,
        test_suite_digest=test_suite_digest,
        test_paths=tuple(files),
        prior_code_review=artifact if verdict is st.ReviewerVerdict.REVISE else None,
    )
    if (
        verdict is st.ReviewerVerdict.REVISE
        and view["prior_code_review"] == st.NO_CODE_REVIEW
    ):
        raise rc.ReviewContractError("REVISE builder view lost the prior review")
    return artifact
