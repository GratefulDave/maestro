"""Rejected candidate findings rebuilt from the authoritative review ledger.

``candidate_reviews`` owns one immutable verdict per candidate SHA. A rejected
verdict and its findings remain visible after repair; a later PASS authorizes
only its own descendant candidate. Legacy review guidance on ``attempts`` is
exposed separately as audit data and never participates in this projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from . import retry_policy as rp
from . import scheduler_types as st


def _name(value: Any) -> Optional[str]:
    if value is None:
        return None
    return getattr(value, "value", value)


@dataclass(frozen=True)
class LocatedFinding:
    """One finding preserved on a rejected candidate review."""

    check_id: str
    object_id: str
    message: str
    grade: str
    scope: str
    status: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "object_id": self.object_id,
            "message": self.message,
            "grade": self.grade,
            "scope": self.scope,
            "status": self.status,
        }


@dataclass(frozen=True)
class CandidateFindings:
    """One immutable rejected candidate and its signed review evidence."""

    review_node_id: str
    candidate_sha: str
    review_digest: str
    receipt_path: str
    findings: Tuple[LocatedFinding, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "review_node_id": self.review_node_id,
            "candidate_sha": self.candidate_sha,
            "review_digest": self.review_digest,
            "receipt_path": self.receipt_path,
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class RunFindings:
    """Every rejected candidate review retained by one run."""

    run_id: str
    declared_outcome: Optional[str]
    reviews: Tuple[CandidateFindings, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "declared_outcome": self.declared_outcome,
            "reviews": [review.as_dict() for review in self.reviews],
        }


def _located(raw: Mapping[str, Any]) -> LocatedFinding:
    return LocatedFinding(
        check_id=str(raw.get("check_id") or ""),
        object_id=str(raw.get("object_id") or ""),
        message=str(raw.get("message") or ""),
        grade=str(raw.get("grade") or ""),
        scope=str(raw.get("scope") or ""),
        status=str(raw.get("status") or ""),
    )


def legacy_findings_from_extra(
    extra: Optional[Mapping[str, Any]],
) -> Tuple[LocatedFinding, ...]:
    """Review-shaped attempt guidance for audit display only.

    Older ledgers stored review output on ``attempts.extra_json``. Returning it
    here keeps that historical row inspectable; candidate dispatch, merge,
    retry, and run-level findings never read this function.
    """
    extra = extra or {}
    guidance = extra.get(rp.GUIDANCE_KEY)
    if not isinstance(guidance, dict) or guidance.get("surface") != "review":
        return ()
    raw = guidance.get("findings")
    if not isinstance(raw, list):
        return ()
    return tuple(_located(item) for item in raw if isinstance(item, dict))


def run_findings(
    run_id: str, reviews: Iterable[Any], *, declared_outcome: Optional[str] = None
) -> RunFindings:
    """Return every terminal REJECTED candidate from ``candidate_reviews``."""
    found = []
    for review in reviews:
        if (
            _name(getattr(review, "state", None))
            != st.CandidateReviewState.COMPLETED.value
        ):
            continue
        if _name(getattr(review, "verdict", None)) != st.ReviewVerdict.REJECTED.value:
            continue
        findings = tuple(
            _located(item)
            for item in getattr(review, "findings", ())
            if isinstance(item, Mapping)
        )
        found.append(
            CandidateFindings(
                review_node_id=str(review.review_node_id),
                candidate_sha=str(review.candidate_sha),
                review_digest=str(review.review_digest or ""),
                receipt_path=str(review.receipt_path or ""),
                findings=findings,
            )
        )
    found.sort(key=lambda item: (item.review_node_id, item.candidate_sha))
    return RunFindings(
        run_id=run_id, declared_outcome=declared_outcome, reviews=tuple(found)
    )


def render(profile: RunFindings) -> str:
    """Render rejected candidates without implying they were merged."""
    outcome = profile.declared_outcome or "(no declared outcome)"
    lines = ["{}  {}".format(profile.run_id, outcome)]
    if not profile.reviews:
        lines.append("  no rejected candidate review carries findings")
        return "\n".join(lines)
    count = len(profile.reviews)
    lines.append(
        "  {} rejected candidate review{}".format(count, "" if count == 1 else "s")
    )
    for review in profile.reviews:
        lines.append(
            "  {}  {}  {} finding{}".format(
                review.review_node_id,
                review.candidate_sha,
                len(review.findings),
                "" if len(review.findings) == 1 else "s",
            )
        )
        if review.review_digest:
            lines.append("    digest   {}".format(review.review_digest))
        if review.receipt_path:
            lines.append("    receipt  {}".format(review.receipt_path))
        for finding in review.findings:
            lines.append("    {}  {}".format(finding.check_id, finding.object_id))
            if finding.message:
                lines.append("      {}".format(finding.message))
    return "\n".join(lines)
