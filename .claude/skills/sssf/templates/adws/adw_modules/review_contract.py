"""The public contract a lane is reviewed against, and the artifact shape.

Does not write lane_state. Canonical identity lives in scheduler_types:
LaneArtifact, ArtifactKind, ReviewerVerdict, canonical_bytes, digest_bytes.

This module was `private_review`, which also owned the token collector and the
redactor that scrubbed every review payload of anything the hidden test suite
contained. Accepted tests are visible now, so nothing here redacts: what
survives is the contract projection, the path normaliser, the gate argv
substitution, and the artifact constructor every lane stage uses.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Sequence, Set, Tuple

from . import scheduler_types as st

_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ReviewContractError(ValueError):
    """A review payload could not be constructed."""


class SuiteEnvironmentError(ReviewContractError):
    """A test suite yielded no usable measurement. Never a candidate defect.

    The fault is the machine, the environment, or the suite itself — never the
    code under test. Two families qualify, and the operator's response to both is
    the same: repair something outside the candidate and resume.

    The suite could not run: the review tree would not provision, the resolved
    runner is unusable, its interpreter does not satisfy the project's declared
    `requires-python`, or no runner can be derived from the accepted files.

    The suite ran but measured nothing: the runner exited cleanly while reporting
    no readable count, or every counted case was skipped. Either way zero
    assertions were evaluated against the candidate.

    In all of them no verdict about the candidate exists, so none may be recorded
    against the builder — a builder cannot fix a suite that never judged it, and
    telling it to try is the burn this class exists to prevent.

    The operator boundary recognises these by class. Membership is what makes that
    recognition structural rather than a match on message text: a renamed code or
    a sixth case must not silently fall through to a traceback.
    """

    code = "SUITE_ENVIRONMENT_REFUSED"


@dataclass(frozen=True)
class LaneRequest:
    """Slice identity needed to fill a LaneArtifact."""

    run_id: str
    lane_id: str
    plan_revision: int
    spec_digest: str
    lane_projection_digest: str
    input_digest: str
    input_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.run_id):
            raise ReviewContractError("run_id is invalid")
        if not _ID.fullmatch(self.lane_id):
            raise ReviewContractError("lane_id is invalid")
        if not isinstance(self.plan_revision, int) or self.plan_revision < 1:
            raise ReviewContractError("plan_revision must be a positive int")
        st.require_hex_digest(self.spec_digest, name="spec_digest")
        st.require_hex_digest(
            self.lane_projection_digest, name="lane_projection_digest"
        )
        st.require_hex_digest(self.input_digest, name="input_digest")
        ids = tuple(str(item) for item in self.input_artifact_ids)
        object.__setattr__(self, "input_artifact_ids", ids)


def make_lane_artifact(
    *,
    kind: st.ArtifactKind,
    request: LaneRequest,
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
    interface: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    criteria = tuple(
        _nonempty(item, "acceptance criterion") for item in acceptance_criteria
    )
    outputs = tuple(normalize_repo_path(item) for item in declared_outputs)
    if not criteria:
        raise ReviewContractError("public_contract requires acceptance_criteria")
    if not outputs:
        raise ReviewContractError("public_contract requires declared_outputs")
    contract = {
        "acceptance_criteria": list(criteria),
        "declared_outputs": list(outputs),
    }
    if interface:
        contract["interface"] = [dict(entry) for entry in interface]
    return contract


def normalize_repo_path(path: str) -> str:
    raw = path.replace("\\", "/")
    if not raw or raw.startswith("/") or raw.endswith("/"):
        raise ReviewContractError("path is not a repository-relative file")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ReviewContractError("path is not a normalized POSIX file")
    return posixpath.normpath(raw)


def _maybe_repo_path(token: str) -> str:
    """`normalize_repo_path` where a non-path token is simply not one."""
    try:
        return normalize_repo_path(token)
    except ReviewContractError:
        return ""


def substituted_gate_argv(
    argv: Sequence[str], files: Iterable[str], tree: Path | str | None = None
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """A gate's argv with its selectors replaced by the files actually written.

    Returns `(argv, selectors)`: the whole invocation, and just the path
    operands within it.

    Every token keeps its authored position. That is the entire point. The
    shape this replaced partitioned argv into `-`-prefixed tokens and
    everything else and then concatenated the two groups, which detaches an
    option from its value: the authored

        --config tests/wp7-checkout/vitest.config.ts tests/wp7-checkout a.test.ts b.test.ts

    became

        --config b.test.ts a.test.ts vitest.config.ts

    so vite loaded a *test file* as its config, evaluated `vitest` outside a
    worker, and refused with `Vitest failed to access its internal state`.
    Every draft the tester wrote was refused identically, because nothing the
    tester could write was the thing that was wrong (measured 2026-09-03,
    FDAdb `lane-wp7-cookie-tests`, four turns).

    A bare token immediately after a `-`-prefixed token carrying no `=` is
    that option's value and is preserved verbatim. A written file the plan did
    not name is appended -- which is what the old fallback to the written set
    was for.

    `tree` is the checkout the argv will run in, and a planned selector the
    draft did not write survives when it exists there. The rule this replaced
    dropped every such selector on the premise that it "names nothing in the
    tree". That premise holds only for a selector naming a file the tester was
    supposed to write and did not; it is false for a gate operand naming a
    file the repository already ships, because both the draft-collection tree
    and the review tree are seeded from the integration ref and carry it.
    Where a plan's floor counts pre-existing cases -- FDAdb
    `lane-wp8r-fixture-tests`, min_cases 16 = 6 authored + the 10 already in
    `src/lib/api/dpa.test.ts` -- dropping the shipped operand made the floor
    unreachable by anything the tester could write. It refused the run
    `DRAFT_MIN_CASES: collected 6, min_cases 16` one turn after the test
    reviewer had correctly told the tester to stop padding the authored file to
    16 with `it.each`; its sibling `lane-wp8r-route-tests` reached its own
    floor of 26 only by generating 18 cases asserting that the other files'
    case titles appear as substrings (measured 2026-09-05, run a2ea7355).

    A caller passing no `tree` cannot test existence and keeps the drop.
    """
    root = Path(tree) if tree is not None else None
    written = tuple(sorted({normalize_repo_path(path) for path in files}))
    rebuilt: List[str] = []
    selectors: List[str] = []
    seen: Set[str] = set()
    expects_value = False
    for token in argv:
        text = str(token)
        if text.startswith("-"):
            rebuilt.append(text)
            expects_value = "=" not in text
            continue
        norm = _maybe_repo_path(text)
        if expects_value:
            expects_value = False
            rebuilt.append(norm if norm in written else text)
            if norm in written:
                seen.add(norm)
            continue
        if norm in written:
            rebuilt.append(norm)
            selectors.append(norm)
            seen.add(norm)
        elif norm and root is not None and (root / norm).is_file():
            # An existing file the gate named. The floor counts it, and the
            # runner can enumerate and execute it. `is_file`, not `exists`: a
            # directory operand is a planned selector whose members the
            # written set already names one by one, and a fresh draft creates
            # its directory itself, so treating one as present would keep a
            # token the wp7-cookie shape is pinned to drop.
            rebuilt.append(norm)
            selectors.append(norm)
        # A planned selector that is neither written nor present names nothing
        # in the tree; carrying it forward is what the written-set fallback
        # existed to avoid.
    for path in written:
        if path in seen:
            continue
        rebuilt.append(path)
        selectors.append(path)
    return tuple(rebuilt), tuple(selectors)


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewContractError("{0} must be a nonempty string".format(label))
    return value.strip()


def as_str_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ReviewContractError("{0} must be a list".format(label))
    return tuple(str(item) for item in value)


def actionable_findings(
    verdict: st.ReviewerVerdict,
    findings: Sequence[Mapping[str, str]],
) -> tuple[Mapping[str, Any], ...]:
    if verdict is st.ReviewerVerdict.PASS:
        if findings:
            raise ReviewContractError("PASS findings must be empty")
        return ()
    if verdict is not st.ReviewerVerdict.REVISE:
        raise ReviewContractError("verdict must be PASS or REVISE")
    return st.require_revise_findings(findings)


def write_files(dest: Path, files: Mapping[str, str]) -> tuple[str, ...]:
    root = Path(dest).resolve()
    written = []
    for path, body in files.items():
        rel = normalize_repo_path(path)
        target = (root / rel).resolve()
        if not str(target).startswith(str(root) + "/") and target != root:
            raise ReviewContractError("refusing path outside destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        written.append(rel)
    if not written:
        raise ReviewContractError("test draft requires at least one file")
    return tuple(written)
