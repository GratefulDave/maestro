"""Compile an authored plan into the shared immutable CompiledPlan."""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence, Tuple

from .plan_model import (
    NO_PLAN_ARTIFACT_REF,
    SCHEMA_VERSION,
    PlanCompileError,
    PlanParseError,
    PlanRefusal,
    normalize_declared_output,
    parse_stored_mapping,
)
from .plan_validate import SCHEMA_INVALID, validate_objective_plan
from .scheduler_types import (
    CompiledPlan,
    LaneProjection,
    canonical_bytes,
    digest_bytes,
    digest_canonical,
    lane_projection_digest,
    topological_integration_order,
)


def compile_plan(
    stored: bytes,
    *,
    plan_revision: int = 1,
    plan_artifact_ref: str = NO_PLAN_ARTIFACT_REF,
) -> CompiledPlan:
    """Admit a plan iff every objective check holds, then freeze its projection.

    ``plan_revision`` and ``plan_artifact_ref`` are supplied by the store.
    ``create_run`` requires ``plan_revision == 1``.
    """
    if plan_revision < 1:
        raise ValueError("plan_revision must be >= 1")
    if not plan_artifact_ref:
        raise ValueError("plan_artifact_ref must be a nonempty sentinel or ref")
    try:
        data = parse_stored_mapping(stored)
    except PlanParseError as exc:
        raise PlanCompileError((PlanRefusal(SCHEMA_INVALID, "/", str(exc)),)) from exc
    refusals = validate_objective_plan(data)
    if refusals:
        raise PlanCompileError(refusals)

    lanes = _lane_projections(data["lanes"])
    plan_bytes = canonical_bytes(_canonical_document(lanes, data["lanes"]))
    return CompiledPlan(
        plan_bytes=plan_bytes,
        plan_artifact_ref=plan_artifact_ref,
        plan_digest=digest_bytes(plan_bytes),
        plan_revision=plan_revision,
        lanes=lanes,
        integration_order=topological_integration_order(lanes),
    )


def _lane_projections(
    raw_lanes: Sequence[Mapping[str, Any]],
) -> Tuple[LaneProjection, ...]:
    compiled: List[LaneProjection] = []
    for raw in raw_lanes:
        needs = tuple(sorted(str(item) for item in raw["needs"]))
        outputs = tuple(sorted(_required_output(item) for item in raw["outputs"]))
        spec_digest = digest_canonical(raw["spec"])
        lane_kind = raw.get("lane_kind")
        compiled.append(
            LaneProjection(
                lane_id=raw["id"],
                needs=needs,
                spec_digest=spec_digest,
                declared_outputs=outputs,
                lane_projection_digest=lane_projection_digest(
                    spec_digest, needs, outputs, lane_kind=lane_kind
                ),
                public_acceptance=tuple(str(item) for item in raw["acceptance"]),
                lane_kind=lane_kind,
            )
        )
    return tuple(sorted(compiled, key=lambda lane: lane.lane_id))


def _required_output(raw: Any) -> str:
    path = normalize_declared_output(raw)
    if path is None:
        raise RuntimeError("validate_objective_plan admitted an invalid output")
    return path


def _canonical_document(
    lanes: Sequence[LaneProjection],
    raw_lanes: Sequence[Mapping[str, Any]],
) -> dict:
    specs = {raw["id"]: raw["spec"] for raw in raw_lanes}
    return {
        "lanes": [
            {
                **(
                    {"lane_kind": lane.lane_kind}
                    if lane.lane_kind is not None
                    else {}
                ),
                "acceptance": list(lane.public_acceptance),
                "id": lane.lane_id,
                "needs": list(lane.needs),
                "outputs": list(lane.declared_outputs),
                "spec": specs[lane.lane_id],
            }
            for lane in lanes
        ],
        "schema_version": SCHEMA_VERSION,
    }
