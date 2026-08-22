"""Merged nodes that carried rejecting findings, rebuilt from the lifecycle ledger.

A run can declare ACCEPTED while every merged lane still holds BLOCKING
review findings. Those findings already persist in two places:

* ``attempts.extra_json.guidance.findings`` (via ``record_review_advisory``)
* ``runs/<run_id>/review/<digest>/findings.json`` on disk

After process exit the only reader was the in-process exit JSON. This
module is the durable reader: it names every MERGED node whose merged
attempt still carries a ``blocking`` finding, and it decides nothing.

Nothing here fails an attempt, blocks a node, or stops a merge (§1.2,
§19 M35). A lifecycle transition may depend only on a counted,
re-derivable fact; a reviewer's prose may advise and may not adjudicate.
The findings are made visible, not powerful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from . import retry_policy as rp


MERGED_NODE_STATE = "MERGED"


def _name(value: Any) -> Optional[str]:
    """An enum member, a bare string, or nothing — as the stored string."""
    if value is None:
        return None
    return getattr(value, "value", value)


def _extra(attempt: Any) -> Mapping[str, Any]:
    extra = getattr(attempt, "extra", None)
    return extra if isinstance(extra, Mapping) else {}


@dataclass(frozen=True)
class LocatedFinding:
    """One rejecting finding, copied from the attempt row's guidance extra."""

    check_id: str
    object_id: str
    message: str
    blocking: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "object_id": self.object_id,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class MergedNodeFindings:
    """One MERGED node whose merged attempt still carries rejecting findings."""

    node_id: str
    attempt_no: int
    subject_digest: str
    findings: Tuple[LocatedFinding, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "attempt_no": self.attempt_no,
            "subject_digest": self.subject_digest,
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class RunFindings:
    """Every merged node of one run that carried a rejecting finding."""

    run_id: str
    declared_outcome: Optional[str]
    nodes: Tuple[MergedNodeFindings, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "declared_outcome": self.declared_outcome,
            "nodes": [node.as_dict() for node in self.nodes],
        }


def blocking_findings_from_extra(
        extra: Optional[Mapping[str, Any]]) -> Tuple[LocatedFinding, ...]:
    """The ``blocking`` findings stored on one attempt's guidance extra.

    Advisories (``blocking`` false or absent) are not rejecting findings and
    are not returned. A missing or non-review surface is empty, never an
    error: this is a reader over rows that predate the key.
    """
    extra = extra or {}
    guidance = extra.get(rp.GUIDANCE_KEY)
    if not isinstance(guidance, dict):
        return ()
    if guidance.get("surface") != "review":
        return ()
    raw = guidance.get("findings")
    if not isinstance(raw, list):
        return ()
    findings: List[LocatedFinding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not item.get("blocking"):
            continue
        findings.append(LocatedFinding(
            check_id=str(item.get("check_id") or ""),
            object_id=str(item.get("object_id") or ""),
            message=str(item.get("message") or ""),
            blocking=True))
    return tuple(findings)


def subject_digest_from_extra(extra: Optional[Mapping[str, Any]]) -> str:
    extra = extra or {}
    guidance = extra.get(rp.GUIDANCE_KEY)
    if isinstance(guidance, dict):
        digest = guidance.get("subject_digest")
        if digest:
            return str(digest)
    digest = extra.get("review_subject_digest")
    return str(digest) if digest else ""


def _attempts_by_node(attempts: Iterable[Any]) -> Dict[str, Dict[int, Any]]:
    by_node: Dict[str, Dict[int, Any]] = {}
    for attempt in attempts:
        node_id = getattr(attempt, "node_id", None)
        attempt_no = getattr(attempt, "attempt_no", None)
        if not node_id or attempt_no is None:
            continue
        by_node.setdefault(str(node_id), {})[int(attempt_no)] = attempt
    return by_node


def run_findings(run_id: str, nodes: Iterable[Any], attempts: Iterable[Any],
                 *, declared_outcome: Optional[str] = None) -> RunFindings:
    """Every MERGED node whose merged attempt carries a rejecting finding.

    The merged attempt is the one whose ``attempt_no`` matches the node's
    current ``attempt_no`` — the attempt that actually merged, not an earlier
    rejection that a later attempt repaired. A skip-merged node with no
    review extra is absent, which is correct: there is nothing to surface.
    """
    by_node = _attempts_by_node(attempts)
    found: List[MergedNodeFindings] = []
    for node in nodes:
        if _name(getattr(node, "state", None)) != MERGED_NODE_STATE:
            continue
        node_id = str(node.node_id)
        attempt_no = int(getattr(node, "attempt_no", 0) or 0)
        attempt = by_node.get(node_id, {}).get(attempt_no)
        extra = _extra(attempt)
        findings = blocking_findings_from_extra(extra)
        if not findings:
            continue
        found.append(MergedNodeFindings(
            node_id=node_id,
            attempt_no=attempt_no,
            subject_digest=subject_digest_from_extra(extra),
            findings=findings))
    found.sort(key=lambda item: item.node_id)
    return RunFindings(
        run_id=run_id, declared_outcome=declared_outcome,
        nodes=tuple(found))


def render(profile: RunFindings) -> str:
    """The operator's view: the verdict, then every rejecting finding beside it."""
    outcome = profile.declared_outcome or "(no declared outcome)"
    lines = ["{}  {}".format(profile.run_id, outcome)]
    if not profile.nodes:
        lines.append("  no merged node carried a rejecting finding")
        return "\n".join(lines)
    count = len(profile.nodes)
    lines.append("  {} merged node{} carried rejecting findings".format(
        count, "" if count == 1 else "s"))
    for node in profile.nodes:
        lines.append("  {}  a{}  {} blocking".format(
            node.node_id, node.attempt_no, len(node.findings)))
        if node.subject_digest:
            lines.append("    digest  {}".format(node.subject_digest))
        for finding in node.findings:
            lines.append("    {}  {}".format(
                finding.check_id, finding.object_id))
            if finding.message:
                lines.append("      {}".format(finding.message))
    return "\n".join(lines)
