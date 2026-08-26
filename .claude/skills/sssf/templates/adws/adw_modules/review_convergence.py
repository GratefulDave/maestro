"""Candidate-review convergence rebuilt from durable lifecycle ledgers.

The unit of review is an immutable candidate SHA, not a scheduler attempt.
``lane_candidates`` supplies stable lane order; ``candidate_reviews`` supplies
exactly one terminal verdict and finding set per reviewed SHA. Attempt extras
and transition prose are intentionally outside this projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import scheduler_types as st


class Outcome(str, Enum):
    CONVERGED = "CONVERGED"
    NOT_CONVERGED = "NOT_CONVERGED"
    NO_REVIEW = "NO_REVIEW"


class Cause(str, Enum):
    REVIEW_CEILING_REACHED = "REVIEW_CEILING_REACHED"
    MERGED_WITHOUT_PASSING_REVIEW = "MERGED_WITHOUT_PASSING_REVIEW"
    RUN_ENDED = "RUN_ENDED"
    RUN_IN_FLIGHT = "RUN_IN_FLIGHT"
    RUN_LIVENESS_UNKNOWN = "RUN_LIVENESS_UNKNOWN"


def _name(value: Any) -> Optional[str]:
    if value is None:
        return None
    return getattr(value, "value", value)


@dataclass(frozen=True)
class CandidateFindingCount:
    candidate_seq: int
    candidate_sha: str
    findings: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidate_seq": self.candidate_seq,
            "candidate_sha": self.candidate_sha,
            "findings": self.findings,
        }


@dataclass(frozen=True)
class LaneConvergence:
    node_id: str
    review_node_id: str
    state: str
    block_reason: Optional[str]
    granted_extra_attempts: int
    findings_per_candidate: Tuple[CandidateFindingCount, ...]
    passed_at_candidate: Optional[int]
    passed_candidate_sha: Optional[str]
    outcome: Outcome
    cause: Optional[Cause]

    @property
    def rejections(self) -> int:
        return len(self.findings_per_candidate)

    @property
    def convergence_length(self) -> Optional[int]:
        return self.rejections + 1 if self.outcome is Outcome.CONVERGED else None

    @property
    def descending(self) -> Optional[bool]:
        counts = [item.findings for item in self.findings_per_candidate]
        if len(counts) < 2:
            return None
        return counts[-1] < counts[0]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "review_node_id": self.review_node_id,
            "state": self.state,
            "block_reason": self.block_reason,
            "granted_extra_attempts": self.granted_extra_attempts,
            "findings_per_candidate": [
                item.as_dict() for item in self.findings_per_candidate
            ],
            "rejections": self.rejections,
            "passed_at_candidate": self.passed_at_candidate,
            "passed_candidate_sha": self.passed_candidate_sha,
            "convergence_length": self.convergence_length,
            "descending": self.descending,
            "outcome": self.outcome.value,
            "cause": self.cause.value if self.cause else None,
        }


@dataclass(frozen=True)
class RunConvergence:
    run_id: str
    review_ceiling: Optional[int]
    lanes: Tuple[LaneConvergence, ...]
    in_flight: Optional[bool] = None

    @property
    def converged(self) -> Tuple[LaneConvergence, ...]:
        return tuple(lane for lane in self.lanes if lane.outcome is Outcome.CONVERGED)

    @property
    def longest(self) -> Optional[LaneConvergence]:
        ranked = sorted(
            self.converged,
            key=lambda lane: (-(lane.convergence_length or 0), lane.node_id),
        )
        return ranked[0] if ranked else None

    @property
    def ceiling_warning(self) -> Optional[str]:
        longest = self.longest
        if longest is None or self.review_ceiling is None:
            return None
        length = longest.convergence_length or 0
        if self.review_ceiling >= length:
            return None
        return (
            "execution.review_ceiling is {ceiling}, and {node} converged only "
            "after {length} candidate reviews in this run; {ceiling} would "
            "have blocked it".format(
                ceiling=self.review_ceiling, node=longest.node_id, length=length
            )
        )

    def as_dict(self) -> Dict[str, Any]:
        longest = self.longest
        return {
            "run_id": self.run_id,
            "review_ceiling": self.review_ceiling,
            "in_flight": self.in_flight,
            "longest_convergence": (
                {
                    "node_id": longest.node_id,
                    "convergence_length": longest.convergence_length,
                }
                if longest is not None
                else None
            ),
            "ceiling_warning": self.ceiling_warning,
            "lanes": [lane.as_dict() for lane in self.lanes],
        }


def _unfinished_cause(in_flight: Optional[bool]) -> Cause:
    if in_flight is True:
        return Cause.RUN_IN_FLIGHT
    if in_flight is False:
        return Cause.RUN_ENDED
    return Cause.RUN_LIVENESS_UNKNOWN


def lane_convergence(
    node: Any,
    candidates: Iterable[Any],
    reviews: Iterable[Any],
    in_flight: Optional[bool] = None,
) -> LaneConvergence:
    node_id = str(node.node_id)
    review_node_id = node_id + "::review"
    ordered = sorted(
        (candidate for candidate in candidates if candidate.build_node_id == node_id),
        key=lambda candidate: candidate.candidate_seq,
    )
    sequence = {
        candidate.candidate_sha: candidate.candidate_seq for candidate in ordered
    }
    terminal = [
        review
        for review in reviews
        if review.review_node_id == review_node_id
        and _name(review.state) == st.CandidateReviewState.COMPLETED.value
    ]
    terminal.sort(key=lambda review: sequence.get(review.candidate_sha, 2**63))

    rejected = tuple(
        CandidateFindingCount(
            candidate_seq=sequence[review.candidate_sha],
            candidate_sha=review.candidate_sha,
            findings=len(review.findings),
        )
        for review in terminal
        if review.candidate_sha in sequence
        and _name(review.verdict) == st.ReviewVerdict.REJECTED.value
    )
    passed = [
        review
        for review in terminal
        if review.candidate_sha in sequence
        and _name(review.verdict) == st.ReviewVerdict.PASS.value
    ]
    passed_review = (
        max(passed, key=lambda review: sequence[review.candidate_sha])
        if passed
        else None
    )
    passed_at = (
        sequence[passed_review.candidate_sha] if passed_review is not None else None
    )
    state = _name(getattr(node, "state", None)) or ""
    block_reason = _name(getattr(node, "block_reason", None))

    if passed_at is not None and (
        not rejected or passed_at > rejected[-1].candidate_seq
    ):
        outcome, cause = Outcome.CONVERGED, None
    elif rejected:
        outcome = Outcome.NOT_CONVERGED
        if block_reason == st.BlockReason.REVIEW_BUDGET_EXHAUSTED.value:
            cause = Cause.REVIEW_CEILING_REACHED
        elif state == st.NodeState.MERGED.value:
            cause = Cause.MERGED_WITHOUT_PASSING_REVIEW
        else:
            cause = _unfinished_cause(in_flight)
    else:
        outcome, cause = Outcome.NO_REVIEW, None

    return LaneConvergence(
        node_id=node_id,
        review_node_id=review_node_id,
        state=state,
        block_reason=block_reason,
        granted_extra_attempts=int(getattr(node, "granted_extra_attempts", 0) or 0),
        findings_per_candidate=rejected,
        passed_at_candidate=passed_at,
        passed_candidate_sha=(
            passed_review.candidate_sha if passed_review is not None else None
        ),
        outcome=outcome,
        cause=cause,
    )


def run_convergence(
    run_id: str,
    nodes: Iterable[Any],
    candidates: Iterable[Any],
    reviews: Iterable[Any],
    review_ceiling: Optional[int] = None,
    in_flight: Optional[bool] = None,
) -> RunConvergence:
    nodes = tuple(nodes)
    candidates = tuple(candidates)
    reviews = tuple(reviews)
    review_parents = {
        str(node.node_id)[: -len("::review")]
        for node in nodes
        if str(node.node_id).endswith("::review")
    }
    lanes = tuple(
        lane_convergence(node, candidates, reviews, in_flight)
        for node in nodes
        if str(node.node_id) in review_parents
    )
    return RunConvergence(
        run_id=run_id, review_ceiling=review_ceiling, lanes=lanes, in_flight=in_flight
    )


def _series_text(lane: LaneConvergence) -> str:
    if not lane.findings_per_candidate:
        return "-"
    return " ".join(
        "c{}:{}".format(item.candidate_seq, item.findings)
        for item in lane.findings_per_candidate
    )


_OUTCOME_TEXT = {
    Outcome.CONVERGED: "converged",
    Outcome.NOT_CONVERGED: "not converged",
    Outcome.NO_REVIEW: "no completed candidate review",
}
_CAUSE_TEXT = {
    Cause.REVIEW_CEILING_REACHED: "review ceiling reached",
    Cause.MERGED_WITHOUT_PASSING_REVIEW: "merged without passing review",
    Cause.RUN_ENDED: "run ended first",
    Cause.RUN_IN_FLIGHT: "run still in flight",
    Cause.RUN_LIVENESS_UNKNOWN: "no later candidate recorded",
}


def render(profile: RunConvergence) -> str:
    lines = [
        "run {}".format(profile.run_id),
        "  review_ceiling {}".format(
            profile.review_ceiling
            if profile.review_ceiling is not None
            else "(unknown)"
        ),
    ]
    for lane in profile.lanes:
        verdict = _OUTCOME_TEXT[lane.outcome]
        if lane.cause is not None:
            verdict += " — " + _CAUSE_TEXT[lane.cause]
        lines.append("  {}  {}  {}".format(lane.node_id, _series_text(lane), verdict))
    if profile.ceiling_warning:
        lines.append("  WARNING: " + profile.ceiling_warning)
    return "\n".join(lines)
