"""Builder view and CODE_REVIEW LaneArtifact. Never writes lane_state.

The builder receives the public contract, architecture constraints, allowed
paths, prior redacted review, and sealed digest. It does not receive private
source, fixtures, selectors, expected literals, vault paths, or private bytes.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
from pathlib import Path
from typing import Mapping, Sequence

from . import bound_surface as bsf
from . import hidden_vault as hv
from . import private_review as pr
from . import runner_resolution as rr
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



# Lines a runner uses to say what went wrong. pytest prefixes assertion output
# with "E ", vitest marks a failing case with a cross and names the error class
# inline. Everything else in a traceback is the test's own source, which is not
# ours to forward.
_FAILURE_MARKERS = (
    "AssertionError",
    "Error:",
    "TypeError",
    "ValueError",
    "expected ",
    "is not a function",
)

# Shapes that identify a test rather than describe a defect. Case names are
# private by the same rule as the test source: pytest names them in its short
# summary and in every node id, vitest in its FAIL header and its file pointer.
# Dropping the whole line is the only safe move -- redaction cannot help,
# because a case name the token set happens to miss would survive intact.
def _names_a_test(line: str) -> bool:
    if "::" in line:
        return True
    if line.startswith(("FAILED", "FAIL ", "PASSED", "✕", "×", "❯", ">")):
        return True
    return line.startswith("tests/") or line.startswith("test_")

# Enough to name every failure in a normal suite without turning the builder
# prompt into a log file.
_MAX_FAILURE_LINES = 40
_MAX_FAILURE_LINE_CHARS = 300

#: A comparison body under a kept failure line: pytest's `Left contains N more
#: items:` block and its dict rendering, vitest's diff. These name the keys that
#: differ, which is the whole answer to a shape mismatch.
_CONTINUATION_PREFIXES = ("+", "-", "{", "}", "'", '"', "E ", "Left ", "Right ", "Omitting", "Full output")

#: Numbers inside a comparison body are the test's expected values, so they are
#: removed even when the token set never saw them. Keys survive, magnitudes do
#: not. Marker lines keep their numbers -- an HTTP status in `expected 400 to be
#: 200` is public API vocabulary, not a fixture value.
_NUMERIC = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")


#: A line that renders a collection literal rather than describing a comparison.
_COLLECTION_PREFIXES = ("{", "}", "'", '"', "+", "-")

#: A diff line whose body is one whole scalar literal. vitest renders a scalar
#: comparison as `- 200` / `+ 400`, which is the same contract vocabulary as the
#: `expected 400 to be 200` marker line above it, not a fixture value. A member
#: of a rendered collection never has this shape: it carries its key
#: (`+   "n": 47`), or its separator (`-   1,`), or its bracket, and the single
#: space is deliberate -- collection members are indented further.
_SCALAR_DIFF_BODY = re.compile(
    r"^[+-] (?:-?\d+(?:\.\d+)?|null|undefined|true|false|NaN)$"
)


def _renders_a_collection(line: str) -> bool:
    # A number inside a rendered collection is a fixture value; a number in a
    # scalar comparison is contract vocabulary. `assert 404 == 200` keeps its
    # status codes because it renders no collection; an assertion that prints a
    # dict loses the numbers inside it even though it is also a marker line.
    # `Left contains 7 more items:` is a header whose number counts keys rather
    # than naming a value, and it is more useful to the builder intact.
    body = line[2:].strip() if line.startswith("E ") else line
    if _SCALAR_DIFF_BODY.match(body.strip()):
        return False
    if body.startswith(_COLLECTION_PREFIXES):
        return True
    return "{" in body or "[" in body


def _is_continuation(raw: str) -> bool:
    if not raw.strip():
        return False
    if raw[:1].isspace():
        return True
    return raw.lstrip().startswith(_CONTINUATION_PREFIXES)


def _bound_surface_names(private_files: Mapping[str, str]) -> tuple[str, ...]:
    """The identifiers `bound_surface` already hands the builder.

    Same derivation, same files, so the two can never disagree about what the
    builder is allowed to see. A failure here is not worth losing the review
    over: an empty tuple simply means nothing is exempt from redaction.
    """
    try:
        surface = bsf.derive_bound_surface(private_files)
    except Exception:
        return ()
    names: set[str] = set()
    for module in surface.get("modules") or ():
        if isinstance(module, Mapping):
            for symbol in module.get("symbols") or ():
                names.add(str(symbol))
    for key in surface.get("object_keys") or ():
        names.add(str(key))
    # A route path is a name the builder is already handed, so redacting it out
    # of the failure line that names it is the one thing that keeps the lane
    # guessing: `assert '[redacted]' == '/internal[redacted]'` hid the whole
    # answer for 20+ rounds on run f50638ab.
    for route in surface.get("routes") or ():
        names.add(str(route))
    return tuple(sorted(name for name in names if name))


def redacted_failure_lines(
    output: str,
    tokens: Sequence[str],
    *,
    allow: Sequence[str] = (),
    limit: int = _MAX_FAILURE_LINES,
) -> tuple[str, ...]:
    """The runner's own failure lines, with every private token redacted.

    The builder is otherwise told only how many cases failed, never which ones,
    so it re-guesses the same fix every round. These lines are what it is
    missing: an error class and the symbol that produced it.

    Safety rides the path `findings` already takes rather than adding a second
    one. Every line goes through `redact_text` over the same token set
    collected from the sealed files, and the caller's existing
    `refuse_private_leak` over the finished payload is the backstop that proves
    nothing from that set survived. Tracebacks are dropped rather than
    redacted: their body is the test's own source, and a redacted skeleton of a
    test is still a shape the builder should not see.
    """
    # Names the builder already holds are not secrets to it. `bound_surface`
    # sends it the module specifiers, exported symbols, and object keys the
    # sealed assertions resolve against, in the same prompt. Redacting those
    # same names here withholds nothing and destroys the signal: an object
    # comparison rendered as `{ [redacted] } to deeply equal { [redacted] }`
    # tells the builder strictly less than the count it already had.
    permitted = {name for name in allow if name}
    tokens = tuple(token for token in tokens if token not in permitted)
    seen: set[str] = set()
    kept: list[str] = []
    in_block = False
    for raw in output.splitlines():
        # vitest colours its diff, so `- Expected` arrives as `\x1b[32m- Expected
        # \x1b[39m` and every test below reads `\x1b` as the first character:
        # `_is_continuation` sees neither leading whitespace nor a prefix, and the
        # whole comparison body is dropped. pytest is unaffected because it emits
        # no colour off a TTY. Strip once, here, so the marker test, the
        # continuation test, and the redaction all read the same text. The
        # expression is `runner_resolution`'s, not a second copy.
        raw = rr._ANSI.sub("", raw)
        line = raw.strip()
        if not line:
            # A blank line separates a comparison body from its header, it does
            # not end it. pytest indents its separator, so the earlier test for
            # a non-empty `raw` kept pytest's dict diffs; vitest emits a truly
            # empty line either side of `- Expected / + Received`, so that test
            # closed the block one line before the diff arrived and dropped
            # every one of them. A blank line now carries no verdict at all --
            # the block still ends where it ended before, at the first line that
            # is neither a marker nor a continuation, and vitest's own `❯`
            # frame is such a line because it names a test.
            continue
        if _names_a_test(line):
            in_block = False
            continue
        marked = line.startswith("E ") or any(
            marker in line for marker in _FAILURE_MARKERS
        )
        continued = in_block and _is_continuation(raw)
        if not marked and not continued:
            in_block = False
            continue
        clean = pr.redact_text(line, tokens)
        if _renders_a_collection(clean):
            # A dict/list rendering: its keys are the answer and its numbers are
            # the test's expected values. `assert 404 == 200` is a continuation
            # line too, and blanking its numbers destroyed the only signal the
            # HTTP cases had -- so the rendering is the narrow target, not every
            # continued line.
            clean = _NUMERIC.sub(pr._REDACTED, clean)
        clean = clean[:_MAX_FAILURE_LINE_CHARS]
        in_block = True
        if clean in seen:
            continue
        seen.add(clean)
        kept.append(clean)
        if len(kept) >= limit:
            break
    return tuple(kept)


#: One line of framing, prepended to the findings the builder is handed when
#: the sealed suite was red. The failure lines come off the runner; the
#: findings are a model's reading of a candidate it saw without the tests, and
#: twice in one session that reading was the exact inverse of the assertion --
#: a builder told to preserve the very keys a sealed case forbids regressed a
#: lane that had been green. Nothing is deleted here: a located finding such
#: as "you wrote outside your declared outputs" is true whatever the suite
#: says. Only the order of authority is stated.
_FINDINGS_FRAMING = (
    "The sealed suite reported failures against your last candidate. Its "
    "failure lines in redacted_failures are ground truth, verbatim from the "
    "runner. The findings below are one model's reading of them, written "
    "without sight of the tests. Where a finding disagrees with a failure "
    "line, the failure line is right."
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
    sealed_digest: str,
    prior_code_review: st.LaneArtifact | None = None,
    private_tokens: Sequence[str] = (),
    allow_names: Sequence[str] = (),
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
        summary = dict(prior_code_review.payload["public_result_summary"])
        findings = list(prior_code_review.payload["findings"])
        if _summary_is_red(summary):
            findings.insert(0, _FINDINGS_FRAMING)
        prior = {
            "findings": findings,
            "public_result_summary": summary,
            "redacted_failures": list(
                prior_code_review.payload.get("redacted_failures") or ()
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
            allow=pr.public_contract_allow(
                contract, extra=(sealed_digest, *allow_names)
            ),
        )
    return view


@dataclasses.dataclass(frozen=True)
class SealedMeasurement:
    """What the sealed suite says about one candidate.

    Measured BEFORE the reviewer agent votes, so the agent can be told that
    the suite is red and how badly. The counts are already public -- they
    ship to the builder as `public_result_summary` -- so handing them to the
    reviewer leaks nothing. What stays private is unchanged: the test source,
    the case names, and the assertion text never leave this module.
    """

    summary: Mapping[str, int]
    runner_failed: bool
    collection_broken: bool
    min_cases: int
    run: Mapping[str, object]
    files: Mapping[str, str]
    vault: Path


def measure_candidate(
    *,
    request: pr.VaultLaneRequest,
    state_root: Path,
    candidate_repo: Path,
    candidate_sha: str,
    candidate_ref: str,
    builder_base_sha: str,
    sealed_bundle: st.LaneArtifact,
    scratch_root: Path,
    allow_candidate_paths: bool = False,
    gate: Mapping[str, object] | object | None = None,
    runtime_root: Path | None = None,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
) -> SealedMeasurement:
    """Run the sealed suite against a candidate and report only counts.

    Split out of `review_builder_output` so the scheduler can measure first
    and hand the reviewer agent the result before it votes. A reviewer told
    "5 of 12 sealed cases fail" reads the diff for a cause; a reviewer told
    nothing returns PASS and the builder learns only that something broke.
    """
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
    return SealedMeasurement(
        summary=summary,
        runner_failed=runner_failed,
        collection_broken=collection_broken,
        min_cases=min_cases,
        run=run,
        files=files,
        vault=vault,
    )


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
    measurement: SealedMeasurement | None = None,
) -> st.LaneArtifact:
    """Bind a reviewer verdict to the sealed measurement of one candidate.

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
            sealed_bundle=sealed_bundle,
            scratch_root=scratch_root,
            allow_candidate_paths=allow_candidate_paths,
            gate=gate,
            runtime_root=runtime_root,
            provision_argv=provision_argv,
            provision_timeout_s=provision_timeout_s,
        )
    vault = measurement.vault
    files = measurement.files
    run = measurement.run
    summary = measurement.summary
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
    private_files = {
        path: hv.cat_blob(vault, blob).decode("utf-8") for path, blob in files.items()
    }
    private_bytes = st.canonical_bytes(
        {
            "counts": run["counts"],
            "executed": run["executed"],
            "output": run["output"],
            "returncode": run["returncode"],
        }
    )
    results_digest = st.digest_bytes(private_bytes)
    # The ref is keyed on the bytes it pins, not on the review's input. The
    # runner's stdout is an observation -- its summary line carries a
    # wall-clock duration -- so two reviews of one input legitimately produce
    # two results, and each gets its own ref. Identical bytes name the
    # identical ref, so pinning is idempotent; different bytes cannot collide.
    # The artifact still carries the ref in `artifact_ref`, which is how a
    # record is found; nothing derives this ref from the input digest.
    results_ref = hv.private_results_ref(
        request.run_id, request.lane_id, results_digest
    )
    tokens = pr.collect_private_tokens(
        files=private_files,
        vault_path=vault,
        vault_refs=(sealed_bundle.artifact_ref, results_ref),
        blob_ids=tuple(files.values()),
    )
    findings_out = pr.actionable_findings(verdict, findings, tokens)
    # Exactly the names the builder is handed as `bound_surface`, derived from
    # the same sealed files. Values are never in here -- the extractor is a
    # whitelist of identifiers, not a filter over text.
    surface_names = _bound_surface_names(private_files)
    failure_lines = redacted_failure_lines(
        str(run["output"]), tokens, allow=surface_names
    )
    results_blob = hv.hash_blob(vault, private_bytes)
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
        "redacted_failures": list(failure_lines),
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
    ) + surface_names
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
        allow_names=surface_names,
    )
    if (
        verdict is st.ReviewerVerdict.REVISE
        and view["prior_code_review"] == st.NO_CODE_REVIEW
    ):
        raise pr.PrivateReviewError("REVISE builder view lost the prior review")
    # Pinned last so a review the checks above refused leaves no ref behind.
    # That is housekeeping, not correctness: the ref is named by the digest of
    # these exact bytes, so wherever this line sits it either creates the ref
    # or finds it already pinned to the same blob. It cannot collide.
    hv.pin_object_ref(vault, results_ref, results_blob)
    return artifact
