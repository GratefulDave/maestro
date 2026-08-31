"""Objective plan checks only. No git, gates, reachability, or admission IR."""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence, Tuple

from .plan_model import (
    LANE_KEYS,
    PLAN_KEYS,
    SCHEMA_VERSION,
    PlanRefusal,
    normalize_declared_output,
    outputs_conflict,
)

SCHEMA_INVALID = "SCHEMA_INVALID"
NEEDS_UNKNOWN = "NEEDS_UNKNOWN"
GRAPH_CYCLE = "GRAPH_CYCLE"
OUTPUT_PATH_INVALID = "OUTPUT_PATH_INVALID"
OUTPUT_OWNERSHIP_CONFLICT = "OUTPUT_OWNERSHIP_CONFLICT"
ACCEPTANCE_MISSING = "ACCEPTANCE_MISSING"
REVIEW_NODE_FORBIDDEN = "REVIEW_NODE_FORBIDDEN"
BUILD_LANE_NEEDS = "BUILD_LANE_NEEDS"


def validate_objective_plan(data: Mapping[str, Any]) -> Tuple[PlanRefusal, ...]:
    """Return every objective refusal. Empty means the mapping is admissible."""
    refusals: List[PlanRefusal] = []
    if set(data) - PLAN_KEYS:
        extra = ", ".join(sorted(set(data) - PLAN_KEYS))
        refusals.append(
            PlanRefusal(
                SCHEMA_INVALID,
                "/",
                "unknown plan field(s): {0}".format(extra),
            )
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        refusals.append(
            PlanRefusal(
                SCHEMA_INVALID,
                "/schema_version",
                "schema_version must be {0}".format(SCHEMA_VERSION),
            )
        )
    lanes = data.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        refusals.append(
            PlanRefusal(SCHEMA_INVALID, "/lanes", "lanes must be a nonempty array")
        )
        return tuple(refusals)

    ids: List[str] = []
    seen = set()
    kinds: dict[str, Optional[str]] = {}
    parsed: List[Tuple[int, str, Sequence[Any], Sequence[Any], Any, Sequence[Any]]] = []
    for index, raw in enumerate(lanes):
        pointer = "/lanes/{0}".format(index)
        if not isinstance(raw, dict):
            refusals.append(
                PlanRefusal(SCHEMA_INVALID, pointer, "lane must be an object")
            )
            continue
        extra = set(raw) - LANE_KEYS
        if extra:
            refusals.append(
                PlanRefusal(
                    SCHEMA_INVALID,
                    pointer,
                    "unknown lane field(s): {0}".format(", ".join(sorted(extra))),
                )
            )
        lane_id = raw.get("id")
        if not isinstance(lane_id, str) or not lane_id:
            refusals.append(
                PlanRefusal(SCHEMA_INVALID, pointer + "/id", "lane id is required")
            )
            continue
        if "::" in lane_id:
            refusals.append(
                PlanRefusal(
                    REVIEW_NODE_FORBIDDEN,
                    pointer + "/id",
                    "review roles are lane stages, never synthetic DAG nodes",
                )
            )
            continue
        if lane_id in seen:
            refusals.append(
                PlanRefusal(SCHEMA_INVALID, pointer + "/id", "duplicate lane id")
            )
            continue
        seen.add(lane_id)
        ids.append(lane_id)
        needs = raw.get("needs", [])
        outputs = raw.get("outputs")
        spec = raw.get("spec")
        acceptance = raw.get("acceptance")
        if not isinstance(needs, list):
            refusals.append(
                PlanRefusal(
                    SCHEMA_INVALID, pointer + "/needs", "needs must be an array"
                )
            )
            needs = []
        if not isinstance(outputs, list) or not outputs:
            refusals.append(
                PlanRefusal(
                    SCHEMA_INVALID,
                    pointer + "/outputs",
                    "outputs must be a nonempty array of file paths",
                )
            )
            outputs = []
        if not isinstance(spec, dict):
            refusals.append(
                PlanRefusal(SCHEMA_INVALID, pointer + "/spec", "spec must be an object")
            )
            spec = {}
        if not isinstance(acceptance, list):
            refusals.append(
                PlanRefusal(
                    SCHEMA_INVALID,
                    pointer + "/acceptance",
                    "acceptance must be an array of public criteria",
                )
            )
            acceptance = []
        if "lane_kind" in raw and raw.get("lane_kind") not in ("tests", "build"):
            refusals.append(
                PlanRefusal(
                    SCHEMA_INVALID,
                    pointer + "/lane_kind",
                    "lane_kind must be tests or build",
                )
            )
        if raw.get("lane_kind") in ("tests", "build"):
            kinds[lane_id] = str(raw["lane_kind"])
        else:
            kinds[lane_id] = None
        parsed.append((index, lane_id, needs, outputs, spec, acceptance))

    id_set = set(ids)
    for index, lane_id, needs, outputs, spec, acceptance in parsed:
        pointer = "/lanes/{0}".format(index)
        seen_needs = set()
        for need_index, need in enumerate(needs):
            need_ptr = pointer + "/needs/{0}".format(need_index)
            if not isinstance(need, str) or not need:
                refusals.append(
                    PlanRefusal(SCHEMA_INVALID, need_ptr, "need id is required")
                )
                continue
            if need == lane_id or need not in id_set:
                refusals.append(
                    PlanRefusal(
                        NEEDS_UNKNOWN if need != lane_id else GRAPH_CYCLE,
                        need_ptr,
                        "every needs id must name another lane in this plan",
                    )
                )
            if need in seen_needs:
                refusals.append(
                    PlanRefusal(SCHEMA_INVALID, need_ptr, "duplicate needs id")
                )
            seen_needs.add(need)
        _validate_outputs(pointer, outputs, refusals)
        _validate_acceptance(pointer, acceptance, refusals)

    _validate_ownership(parsed, refusals)
    _validate_build_lane_needs(parsed, kinds, refusals)
    if not any(item.code == GRAPH_CYCLE for item in refusals):
        refusals.extend(_cycles(parsed))
    return tuple(refusals)


def _validate_outputs(
    pointer: str, outputs: Sequence[Any], refusals: List[PlanRefusal]
) -> None:
    seen = set()
    for index, raw in enumerate(outputs):
        out_ptr = pointer + "/outputs/{0}".format(index)
        normalized = normalize_declared_output(raw)
        if normalized is None:
            refusals.append(
                PlanRefusal(
                    OUTPUT_PATH_INVALID,
                    out_ptr,
                    "declared output must be an exact repository-relative POSIX file path",
                )
            )
            continue
        if normalized in seen:
            refusals.append(
                PlanRefusal(
                    OUTPUT_OWNERSHIP_CONFLICT,
                    out_ptr,
                    "duplicate declared output",
                )
            )
        seen.add(normalized)


def _validate_acceptance(
    pointer: str, acceptance: Sequence[Any], refusals: List[PlanRefusal]
) -> None:
    if not acceptance:
        refusals.append(
            PlanRefusal(
                ACCEPTANCE_MISSING,
                pointer + "/acceptance",
                "each lane must declare public acceptance criteria",
            )
        )
        return
    for index, item in enumerate(acceptance):
        if not isinstance(item, str) or not item.strip():
            refusals.append(
                PlanRefusal(
                    ACCEPTANCE_MISSING,
                    pointer + "/acceptance/{0}".format(index),
                    "each public acceptance criterion must be a nonempty string",
                )
            )


def _validate_ownership(
    parsed: Sequence[Tuple[int, str, Sequence[Any], Sequence[Any], Any, Sequence[Any]]],
    refusals: List[PlanRefusal],
) -> None:
    owned: List[Tuple[str, str, str]] = []
    for index, lane_id, _needs, outputs, _spec, _acceptance in parsed:
        for out_index, raw in enumerate(outputs):
            normalized = normalize_declared_output(raw)
            if normalized is None:
                continue
            owned.append(
                (
                    normalized,
                    lane_id,
                    "/lanes/{0}/outputs/{1}".format(index, out_index),
                )
            )
    for left_i, (left_path, left_lane, left_ptr) in enumerate(owned):
        for right_path, right_lane, right_ptr in owned[left_i + 1 :]:
            if left_lane == right_lane:
                continue
            if outputs_conflict(left_path, right_path):
                refusals.append(
                    PlanRefusal(
                        OUTPUT_OWNERSHIP_CONFLICT,
                        right_ptr,
                        "path {0} conflicts with {1} owned by {2}".format(
                            right_path, left_path, left_lane
                        ),
                    )
                )


def _validate_build_lane_needs(
    parsed: Sequence[Tuple[int, str, Sequence[Any], Sequence[Any], Any, Sequence[Any]]],
    kinds: Mapping[str, str | None],
    refusals: List[PlanRefusal],
) -> None:
    for index, lane_id, needs, _outputs, _spec, _acceptance in parsed:
        if kinds.get(lane_id) != "build":
            continue
        pointer = "/lanes/{0}".format(index)
        test_indexes = [
            need_index
            for need_index, need in enumerate(needs)
            if isinstance(need, str) and kinds.get(need) == "tests"
        ]
        if len(test_indexes) != 1:
            if len(test_indexes) > 1:
                need_ptr = pointer + "/needs/{0}".format(test_indexes[1])
            else:
                need_ptr = pointer + "/needs"
            refusals.append(
                PlanRefusal(
                    BUILD_LANE_NEEDS,
                    need_ptr,
                    "build lane must have exactly one tests dependency",
                )
            )
        for need_index, need in enumerate(needs):
            if not isinstance(need, str) or not need:
                continue
            dep_kind = kinds.get(need)
            if dep_kind == "tests" or dep_kind == "build":
                continue
            refusals.append(
                PlanRefusal(
                    BUILD_LANE_NEEDS,
                    pointer + "/needs/{0}".format(need_index),
                    "build lane extra needs must be build lanes",
                )
            )


def _cycles(
    parsed: Sequence[Tuple[int, str, Sequence[Any], Sequence[Any], Any, Sequence[Any]]],
) -> Tuple[PlanRefusal, ...]:
    graph = {
        lane_id: [need for need in needs if isinstance(need, str)]
        for _index, lane_id, needs, _outputs, _spec, _acceptance in parsed
    }
    visiting: List[str] = []
    seen = set()
    found: List[PlanRefusal] = []

    def walk(node: str) -> None:
        if node in seen or node not in graph:
            return
        if node in visiting:
            found.append(
                PlanRefusal(
                    GRAPH_CYCLE,
                    "/lanes",
                    "dependency cycle: {0}".format(" -> ".join(visiting + [node])),
                )
            )
            return
        visiting.append(node)
        for need in graph[node]:
            walk(need)
        visiting.pop()
        seen.add(node)

    for lane_id in sorted(graph):
        walk(lane_id)
    return tuple(found)
