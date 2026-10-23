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
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Mapping, Sequence

from . import probes
from . import provisioning as prov
from . import remote_suite as rs
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

#: A green-suite REVISE whose findings were all on the standards axis, or
#: absent, after the scheduler's second ask. The standards findings cannot
#: gate, so they are gone; the reviewer's REVISE stands, and this says why
#: the builder is holding no finding of the reviewer's own.
_UNGATED_REVISE = {
    "implementation_area": "declared product outputs",
    "observed_behavior": (
        "the code reviewer returned REVISE twice with no finding against the "
        "public contract; standards findings cannot send a lane back"
    ),
    "required_behavior": (
        "the candidate must satisfy every public_contract acceptance criterion"
    ),
    "violated_requirement": "the code reviewer grades against public_contract",
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
    suite_timeout_s: float | None = None,
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
    _review_tree(integration_repo, integration_sha, dest, state_root=state_root)
    # `failed` here is read by `FactoryScheduler._failed_run_gates`, which
    # stands between a merged surface and both the final review and
    # publication. A gate that measured bytes nobody accepted is a
    # publication decision made on a forgery, so the tree is verified first.
    tb.verify_suite(files, dest)
    run = _remote_suite(
        dest, files, gate, suite_timeout_s, provision_argv, provision_timeout_s
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
# `remote_job.execute` calls it through the module so one patch of `prov.provision_tree`
# observes every site.
ReviewProvisioningError = prov.ReviewProvisioningError
provision_tree = prov.provision_tree


def _review_tree(repo: Path, sha: str, dest: Path, *, state_root: Path) -> Path:
    """The commit under review, unprovisioned: its suite runs remotely.

    Provisioning happens where the suite runs (`remote_job.execute`), on the
    spot fleet, so nothing is installed into this tree on the scheduler host.
    """
    dest = Path(dest)
    if dest.exists() and dest.is_dir() and any(dest.iterdir()):
        return tm.refresh_materialized_commit(repo, sha, dest, state_root=state_root)
    return tm.materialize_commit(repo, sha, dest, state_root=state_root)


def _remote_suite(
    tree: Path,
    files: Mapping[str, str],
    gate: Mapping[str, object] | object | None,
    suite_timeout_s: float | None,
    provision_argv: Sequence[str],
    provision_timeout_s: float | None,
) -> Mapping[str, object]:
    """`tests_chain.run_suite` against `tree`, run on the spot fleet."""
    return rs.call(
        "suite",
        tree,
        {"paths": list(files), "gate": gate, "timeout_s": suite_timeout_s},
        provision_argv=provision_argv,
        provision_timeout_s=provision_timeout_s,
        timeout_s=tc.SUITE_TIMEOUT_S if suite_timeout_s is None else suite_timeout_s,
    )


def _suite_run_path(
    state_root: Path,
    request: rc.LaneRequest,
    candidate_sha: str,
    files: Mapping[str, str],
    gate: object,
    provision_argv: Sequence[str],
    suite_timeout_s: float | None,
) -> Path:
    """Where a finished measurement of exactly these inputs is kept.

    A scheduler interrupted mid-suite pauses the lane, and the worker that was
    running the suite finishes anyway (its late completion is refused by the
    ledger). The next session measures the same candidate again, 10-17 minutes
    for a result that already existed. Everything the measurement depends on is
    in the key, so a different candidate, suite, gate or provisioning never
    reads it.
    """
    key = hashlib.sha256(
        json.dumps(
            [
                request.input_digest,
                candidate_sha,
                sorted(files.items()),
                repr(gate),
                list(provision_argv),
                suite_timeout_s,
            ],
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return Path(state_root) / "suite-measurements" / (key + ".json")


def _load_suite_run(path: Path) -> Mapping[str, object] | None:
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return run if isinstance(run, dict) and "counts" in run else None


def _store_suite_run(path: Path, run: Mapping[str, object]) -> None:
    """Keep a finished measurement; a crashed run is not one."""
    if run.get("crashed") or run.get("crash_note"):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".{0}.tmp".format(os.getpid()))
        temp.write_text(json.dumps(run, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    except (OSError, TypeError, ValueError):
        pass


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
    suite_timeout_s: float | None = None,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
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
        return (
            _remote_suite(
                tree, files, gate, suite_timeout_s, provision_argv, provision_timeout_s
            ),
            None,
        )
    except rc.SuiteTimeoutError:
        # A run that did not finish is not "no case outcome": it must reach the
        # operator, not the base-collection check that would read it as one.
        raise
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
    suite_timeout_s: float | None = None,
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
        _review_tree(candidate_repo, builder_base_sha, base_dest, state_root=state_root)
        tb.verify_suite(files, base_dest)
        run, refusal = _run_suite(
            base_dest,
            files,
            gate=gate,
            suite_timeout_s=suite_timeout_s,
            provision_argv=provision_argv,
            provision_timeout_s=provision_timeout_s,
        )
    except (tm.TreeContainmentRefused, tb.TestSuiteTampered, rc.SuiteTimeoutError):
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


#: pytest's `-vv` outcome line and its short-summary line, and vitest's FAIL
#: header. Each names one failing case by the id the runner itself prints.
_PYTEST_FAILED_CASE = re.compile(r"^(\S+::\S+) (?:FAILED|ERROR)\b")
_PYTEST_SUMMARY_CASE = re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)")
_VITEST_FAILED_CASE = re.compile(r"^FAIL\s+(\S.*? > .*?)(?:\s+\d+(?:\.\d+)?m?s)?$")


def failing_case_ids(output: str) -> tuple[str, ...]:
    """Every case id the runner reported failing or erroring, in order, once.

    The id is the runner's own: `path::name` for pytest, `path > ... > title`
    for vitest. A code reviewer is handed these and must account for each by a
    finding's `test_id`, so a failing case can no longer pass through a REVISE
    that never mentions it.
    """
    seen: dict[str, None] = {}
    for raw in output.splitlines():
        line = rr._ANSI.sub("", raw).strip()
        for pattern in (_PYTEST_FAILED_CASE, _PYTEST_SUMMARY_CASE, _VITEST_FAILED_CASE):
            match = pattern.match(line)
            if match:
                seen.setdefault(match.group(1), None)
                break
    return tuple(seen)


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


#: Prepended to the findings the builder is handed when any of them was
#: synthesized from a probe the harness executed. Unlike the suite framing it
#: does not depend on the suite being red: a probe fails on a green suite.
_PROBE_FRAMING = (
    "Findings coded PROBE_FAILED come from a probe file the harness executed "
    "against your last candidate. The lines in failure_output that begin "
    "[probe <path>] are the runner's own output and are ground truth. The probe "
    "source is in probes; it is read-only and you may not edit, move or delete "
    "it. Make its cases pass by changing the product outputs."
)


def builder_view(
    *,
    public_contract: Mapping[str, object],
    architecture_constraints: Sequence[str],
    test_suite_digest: str,
    test_paths: Sequence[str] = (),
    prior_code_review: st.LaneArtifact | None = None,
    probe_texts: Mapping[str, str] | None = None,
) -> dict:
    contract = rc.public_contract(
        acceptance_criteria=rc.as_str_tuple(
            public_contract["acceptance_criteria"], "acceptance_criteria"
        ),
        declared_outputs=rc.as_str_tuple(
            public_contract["declared_outputs"], "declared_outputs"
        ),
        interface=public_contract.get("interface") or (),
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
        if any(
            isinstance(item, Mapping) and item.get("code") == st.PROBE_FAILED
            for item in findings
        ):
            findings.insert(0, _PROBE_FRAMING)
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
    if prior_code_review is not None and prior_code_review.payload.get("probe_records"):
        payload = prior_code_review.payload
        status = payload.get("probe_status") or {}
        texts = probe_texts or {}
        view["probes"] = [
            {
                "intent": record["intent"],
                "path": record["path"],
                "probe_id": record["probe_id"],
                "requirement": record["requirement"],
                "source": texts.get(record["path"], ""),
                "status": status.get(record["probe_id"], probes.STATUS_OPEN),
            }
            for record in probes.active_records(
                payload["probe_records"], payload.get("probe_retired") or ()
            )
        ]
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
    #: The materialized, provisioned review tree the suite ran in. Probes are
    #: written into and run from this same tree.
    tree: Path | None = None
    #: The runner's own ids of every case that failed or errored.
    failing_test_ids: tuple[str, ...] = ()


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
    suite_timeout_s: float | None = None,
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
    tree = _review_tree(candidate_repo, candidate_sha, dest, state_root=state_root)
    tb.verify_suite(files, dest)
    kept = _suite_run_path(
        state_root, request, candidate_sha, files, gate, provision_argv,
        suite_timeout_s,
    )
    run, refusal = _load_suite_run(kept), None
    if run is None:
        run, refusal = _run_suite(
            dest,
            files,
            gate=gate,
            suite_timeout_s=suite_timeout_s,
            provision_argv=provision_argv,
            provision_timeout_s=provision_timeout_s,
        )
        if refusal is None:
            _store_suite_run(kept, run)
    collection_broken = False
    crashed = str(run.get("crashed") or "")
    # A run killed by a signal is already typed (`SUITE_CRASHED:<signal>`), so
    # it is not the ambiguous "no case outcome" the base comparison settles.
    if not crashed and (refusal is not None or _collected_no_case(run)):
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
            suite_timeout_s=suite_timeout_s,
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
    if crashed:
        # Present only on a crash, so every other summary is byte-identical.
        summary["crashed"] = crashed
        summary["crash_tail"] = list(run.get("crash_tail") or ())
    elif run.get("crash_note"):
        # The runner died by signal after every case had an outcome. The
        # counts above are those outcomes; the crash is reported, not counted.
        summary["crash_note"] = str(run["crash_note"])
        summary["crash_tail"] = list(run.get("crash_tail") or ())
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
        tree=tree,
        failing_test_ids=() if crashed else failing_case_ids(str(run["output"])),
    )


@dataclasses.dataclass(frozen=True)
class ProbeMeasurement:
    """The probes of one review: the cumulative record and this review's reading.

    `records`, `retired` and `receipts` are cumulative and immutable -- every
    CODE_REVIEW carries the full lists so far. `status` and `evidence` are what
    THIS review measured for each ACTIVE probe (records minus retired).
    `submissions` is this review's per-submission outcome list.
    """

    records: tuple[Mapping[str, object], ...] = ()
    retired: tuple[Mapping[str, object], ...] = ()
    receipts: tuple[Mapping[str, object], ...] = ()
    submissions: tuple[Mapping[str, object], ...] = ()
    status: Mapping[str, str] = dataclasses.field(default_factory=dict)
    evidence: Mapping[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)
    commit: str = ""
    ref: str = ""

    def is_empty(self) -> bool:
        return not (self.records or self.retired or self.receipts or self.submissions)


_DRIFT_IDS = 3


def _drift_line(frozen: Sequence[str], seen: Mapping[str, str]) -> str:
    missing = sorted(set(frozen) - set(seen))[:_DRIFT_IDS]
    extra = sorted(set(seen) - set(frozen))[:_DRIFT_IDS]
    return "probe cases changed: missing {0}, extra {1}".format(missing, extra)


def measure_probes(
    *,
    tree: Path,
    records: Sequence[Mapping[str, object]],
    retired: Sequence[Mapping[str, object]] = (),
    receipts: Sequence[Mapping[str, object]] = (),
    submissions: Sequence[Mapping[str, object]] = (),
    scratch_root: Path,
    commit: str = "",
    ref: str = "",
    timeout_s: float = rr.EXECUTE_TIMEOUT_S,
    provision_argv: Sequence[str] = (),
    provision_timeout_s: float | None = None,
) -> ProbeMeasurement:
    """Execute every ACTIVE probe once against the review tree.

    The probe files must already be in `tree` (`probes.write_probe_tree`). A
    probe is DISCHARGED only by `probes.discharge` over its single execution
    against its frozen case ids; anything else -- a failing case, a runner
    failure, a missing or partial report, collection drift, an unusable
    runner -- is OPEN with the evidence that says why. Candidate-dependent
    case growth earns nothing: the frozen set is fixed at admission.
    """
    probes.validate_receipts(records, retired, receipts)
    status: dict[str, str] = {}
    evidence: dict[str, tuple[str, ...]] = {}
    active = probes.active_records(records, retired)
    # One remote run per runner: it resolves the runner over every active probe
    # of that runner, then executes each once, on the spot fleet.
    by_runner: dict[str, list[str]] = {}
    for record in active:
        runner = tc._file_runner(str(record["path"]))
        if runner is not None:
            by_runner.setdefault(runner, []).append(str(record["path"]))
    executions: dict[str, rr.ProbeExecution | str] = {}
    for runner, paths in by_runner.items():
        try:
            executions.update(
                rs.call(
                    "probes",
                    Path(tree),
                    {"runner": runner, "peers": paths, "paths": paths, "timeout_s": timeout_s},
                    provision_argv=provision_argv,
                    provision_timeout_s=provision_timeout_s,
                    timeout_s=rr.PROBE_TIMEOUT_S + timeout_s * len(paths),
                )
            )
        except rr.RunnerUnusable as extra:
            reason = "runner unusable: {0}".format(
                getattr(extra, "detail", "") or extra
            )[:300]
            executions.update({path: reason for path in paths})
    del scratch_root  # scratch is allocated where the probes run
    for record in active:
        pid = str(record["probe_id"])
        path = str(record["path"])
        if tc._file_runner(path) is None:
            status[pid], evidence[pid] = probes.STATUS_OPEN, ("probe names no runner",)
            continue
        execution = executions[path]
        if isinstance(execution, str):
            status[pid], evidence[pid] = probes.STATUS_OPEN, (execution,)
            continue
        frozen = [str(item) for item in record["case_ids"]]
        if probes.discharge(execution, frozen):
            status[pid], evidence[pid] = probes.STATUS_DISCHARGED, ()
            continue
        lines = list(failure_lines(execution.output))
        if execution.report_error:
            lines.append(
                "probe run incomplete: {0} (exit {1})".format(
                    execution.report_error, execution.returncode
                )
            )
        elif set(execution.cases) != set(frozen):
            lines.append(_drift_line(frozen, execution.cases))
        elif execution.returncode != 0 and not lines:
            lines.append("probe run exited {0}".format(execution.returncode))
        status[pid] = probes.STATUS_OPEN
        evidence[pid] = tuple(lines) or ("probe cases did not all pass",)
    return ProbeMeasurement(
        records=tuple(records),
        retired=tuple(retired),
        receipts=tuple(receipts),
        submissions=tuple(submissions),
        status=status,
        evidence=evidence,
        commit=commit,
        ref=ref,
    )


def review_progress(payload: Mapping[str, object]) -> object:
    """What a build round moved, for the stall rules and for the gate tool.

    A tuple, never a sum: `(suite_passed, discharged ids, active ids)`. A
    discharged probe cannot compensate for a suite regression, and a probe
    that was discharged and is open again is its own regression -- both are
    read off the tuple's parts. A review that never carried a probe returns
    the plain `passed` integer, so every existing history is unchanged.
    """
    summary = payload.get("public_result_summary") or {}
    passed = summary.get("passed")
    records = payload.get("probe_records")
    if not records:
        return passed
    retired = {entry.get("probe_id") for entry in payload.get("probe_retired") or ()}
    active = sorted(
        str(r["probe_id"]) for r in records if r.get("probe_id") not in retired
    )
    status = payload.get("probe_status") or {}
    discharged = [pid for pid in active if status.get(pid) == probes.STATUS_DISCHARGED]
    return (passed, tuple(discharged), tuple(active))


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
    suite_timeout_s: float | None = None,
    measurement: SuiteMeasurement | None = None,
    probe_measurement: ProbeMeasurement | None = None,
    suite_defect_claims: Sequence[Mapping[str, str]] = (),
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
            suite_timeout_s=suite_timeout_s,
        )
    files = measurement.files
    run = measurement.run
    summary = measurement.summary
    # The two axes are separated before anything reads a verdict off them. A
    # standards finding is a hygiene judgement call: it is capped at WARNING,
    # it is recorded in `advisory_findings`, and it is not in the set that
    # decides whether this lane goes back to BUILDING. Nothing below ranks one
    # axis against the other -- they simply never meet again.
    for submitted in findings:
        if any(key in submitted for key in st.HARNESS_FINDING_KEYS):
            raise rc.ReviewContractError(
                "REVIEW_FINDINGS_MALFORMED: a reviewer finding may not carry a code"
            )
    findings, standards = st.partition_findings_by_axis(findings)
    probe_findings: tuple[Mapping[str, str], ...] = ()
    probe_lines: list[str] = []
    probed = probe_measurement is not None and not probe_measurement.is_empty()
    if probed:
        probes.validate_receipts(
            probe_measurement.records, probe_measurement.retired, probe_measurement.receipts
        )
        for record in probes.active_records(
            probe_measurement.records, probe_measurement.retired
        ):
            pid = str(record["probe_id"])
            if probe_measurement.status.get(pid) == probes.STATUS_DISCHARGED:
                continue
            lines = probe_measurement.evidence.get(pid) or ()
            probe_findings += (probes.probe_failed_finding(record, lines),)
            probe_lines.extend(
                "[probe {0}] {1}".format(record["path"], line) for line in lines
            )
    if measurement.runner_failed and verdict is st.ReviewerVerdict.PASS:
        verdict = st.ReviewerVerdict.REVISE
    if probe_findings and verdict is st.ReviewerVerdict.PASS:
        # One direction only, like the runner promotion above: a measured
        # failing probe is a REVISE whatever the reviewer's vote, and whatever
        # the probe budget says. It is never rewritten the other way.
        verdict = st.ReviewerVerdict.REVISE
    # Appended BEFORE the empty-findings check so `_UNGATED_REVISE` can never
    # stand in for a located, measured failure.
    findings = tuple(findings) + probe_findings
    if verdict is st.ReviewerVerdict.REVISE and not findings:
        # A REVISE with nothing locatable left: a red suite the reviewer did
        # not locate, or a REVISE whose findings were all standards. Say what
        # happened rather than nothing; the scheduler has already asked it a
        # second time by the time this fires. The reviewer's REVISE is never
        # rewritten to PASS here.
        if measurement.runner_failed:
            findings = (
                _COLLECTION_REVISE if measurement.collection_broken else _RUNNER_REVISE,
            )
        else:
            findings = (_UNGATED_REVISE,)
    digest_body: dict[str, object] = {
        "counts": run["counts"],
        "executed": run["executed"],
        "output": run["output"],
        "returncode": run["returncode"],
    }
    if probed:
        digest_body["probes"] = {
            "evidence": {k: list(v) for k, v in probe_measurement.evidence.items()},
            "records": [r["probe_id"] for r in probe_measurement.records],
            "retired": [r["probe_id"] for r in probe_measurement.retired],
            "status": dict(probe_measurement.status),
        }
    results_digest = st.digest_bytes(st.canonical_bytes(digest_body))
    findings_out = rc.actionable_findings(verdict, findings)
    test_suite_digest = str(accepted_suite.payload["test_suite_digest"])
    payload = {
        "builder_base_sha": builder_base_sha,
        "candidate_ref": candidate_ref,
        "candidate_sha": candidate_sha,
        "failure_output": (
            [str(run["crashed"]), *run.get("crash_tail", ())]
            if run.get("crashed")
            else list(failure_lines(str(run["output"])))
            + (
                [str(run["crash_note"]), *run.get("crash_tail", ())]
                if run.get("crash_note")
                else []
            )
        )
        + probe_lines,
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
    # Each only when there is something to record, so every other review is
    # byte-identical. `_suite_reopen_plan` reads both: a finding attributed to
    # the accepted suite by `test_id` counts only for an id named here.
    if measurement.failing_test_ids:
        payload["failing_test_ids"] = list(measurement.failing_test_ids)
    if suite_defect_claims:
        payload["suite_defect_claims"] = [dict(item) for item in suite_defect_claims]
    if standards:
        # Recorded, never dropped: the builder reads only `findings`, so
        # these reach no builder and cause no transition, but the review
        # keeps what its reviewer said.
        payload["advisory_findings"] = [
            {
                key: str(item[key])
                for key in (*st.REVISE_FINDING_KEYS, *st.FINDING_OPTIONAL_KEYS)
                if key in item
            }
            for item in standards
        ]
    if probed:
        active_ids = [
            r["probe_id"]
            for r in probes.active_records(
                probe_measurement.records, probe_measurement.retired
            )
        ]
        payload["probe_records"] = [dict(r) for r in probe_measurement.records]
        payload["probe_retired"] = [dict(r) for r in probe_measurement.retired]
        payload["probe_receipts"] = [dict(r) for r in probe_measurement.receipts]
        payload["probe_submissions"] = [dict(r) for r in probe_measurement.submissions]
        payload["probe_status"] = dict(probe_measurement.status)
        payload["probe_summary"] = {
            "active": len(active_ids),
            "discharged": sum(
                1
                for pid in active_ids
                if probe_measurement.status.get(pid) == probes.STATUS_DISCHARGED
            ),
            "open": sum(
                1
                for pid in active_ids
                if probe_measurement.status.get(pid) != probes.STATUS_DISCHARGED
            ),
        }
        if probe_measurement.commit:
            payload["probe_commit"] = probe_measurement.commit
        if probe_measurement.ref:
            payload["probe_ref"] = probe_measurement.ref
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
