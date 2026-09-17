"""Objective plan checks only. No git, gates, reachability, or admission IR."""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence, Tuple

from .plan_model import (
    LANE_KEYS,
    PLAN_KEYS,
    SCHEMA_VERSION,
    PlanRefusal,
    consumer_problems,
    decided_by_problems,
    interface_problems,
    restriction_problems,
    normalize_declared_output,
    outputs_conflict,
    parse_acceptance_item,
)

SCHEMA_INVALID = "SCHEMA_INVALID"
NEEDS_UNKNOWN = "NEEDS_UNKNOWN"
GRAPH_CYCLE = "GRAPH_CYCLE"
OUTPUT_PATH_INVALID = "OUTPUT_PATH_INVALID"
OUTPUT_OWNERSHIP_CONFLICT = "OUTPUT_OWNERSHIP_CONFLICT"
OUTPUT_OVERLAPS_TEST_SUITE = "OUTPUT_OVERLAPS_TEST_SUITE"
ACCEPTANCE_MISSING = "ACCEPTANCE_MISSING"
REVIEW_NODE_FORBIDDEN = "REVIEW_NODE_FORBIDDEN"
BUILD_LANE_NEEDS = "BUILD_LANE_NEEDS"
OBLIGATION_UNOBSERVABLE = "OBLIGATION_UNOBSERVABLE"
OBLIGATION_UNDECIDED = "OBLIGATION_UNDECIDED"
CASE_FALSIFICATION_UNDECLARED = "CASE_FALSIFICATION_UNDECLARED"
INTERFACE_UNDECLARED = "INTERFACE_UNDECLARED"
INTERFACE_UNCONSUMED = "INTERFACE_UNCONSUMED"


def validate_objective_plan(
    data: Mapping[str, Any], *, bound_run: bool = False
) -> Tuple[PlanRefusal, ...]:
    """Return every objective refusal. Empty means the mapping is admissible.

    ``bound_run`` is true only when re-reading the plan revision a run is
    already bound to (``run resume``/``status``/``attend`` and the previous
    revision under ``run amend``). ``OBLIGATION_UNOBSERVABLE`` and
    ``OBLIGATION_UNDECIDED`` are authoring obligations judged when a plan is
    shipped, started, or amended into a run; neither is re-judged against a
    revision a run already holds, so a run bound before either check existed
    is never refused mid-run. Every strict path keeps both.
    """
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
        _validate_acceptance(pointer, acceptance, refusals, bound_run=bound_run)
        _validate_declared_cases(
            pointer, spec, kinds.get(lane_id), refusals, bound_run=bound_run
        )
        _validate_interface(
            pointer, spec, kinds, lane_id, needs, refusals, bound_run=bound_run
        )

    _validate_ownership(parsed, refusals)
    _validate_test_suite_outputs(parsed, kinds, refusals)
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
    pointer: str,
    acceptance: Sequence[Any],
    refusals: List[PlanRefusal],
    *,
    bound_run: bool = False,
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
        item_pointer = pointer + "/acceptance/{0}".format(index)
        parsed = parse_acceptance_item(item)
        if parsed is None:
            refusals.append(
                PlanRefusal(
                    ACCEPTANCE_MISSING,
                    item_pointer,
                    "each public acceptance criterion must be a nonempty string "
                    "or an object with a nonempty criterion",
                )
            )
            continue
        if bound_run:
            continue
        if parsed.gating and not parsed.observation_seam:
            refusals.append(
                PlanRefusal(
                    OBLIGATION_UNOBSERVABLE,
                    item_pointer,
                    "a gating obligation must declare the observation_seam a "
                    "case can assert on from the public contract; an obligation "
                    "no test can observe is advisory, not gating",
                )
            )
        if not (parsed.gating or parsed.decided_by is not None):
            continue
        problems = restriction_problems(parsed.restriction) if parsed.gating else ()
        for problem in problems + decided_by_problems(
            parsed.decided_by, refusal_required=parsed.refusal_required
        ):
            refusals.append(
                PlanRefusal(
                    OBLIGATION_UNDECIDED,
                    item_pointer + "/decided_by",
                    "obligation {0!r} {1}; a gating obligation states its "
                    "expected answers as exact worked examples".format(
                        parsed.criterion, problem
                    ),
                )
            )


def _validate_declared_cases(
    pointer: str,
    spec: Mapping[str, Any],
    lane_kind: Any,
    refusals: List[PlanRefusal],
    *,
    bound_run: bool = False,
) -> None:
    """A tests lane's declared cases, and which of them must be red.

    "The suite is red at the parent" is not falsification. FDAdb run be064e58
    `lane-wp3-adapter-build` burned three code-review rounds and parked
    `NO_PROGRESS` on an accepted suite reading `executed=6 passed=5 failed=1`,
    identically, from three different candidates. The red case asserted a
    non-regression property -- it should have been GREEN at the parent -- and
    its final assertion called a shipped module the lane did not own, with a
    key that module does not recognise. No change the builder was allowed to
    make could ever have moved it, and "red at the parent" was satisfied the
    whole time.

    So the plan states the outcome it expects from each case BEFORE any builder
    exists, and the harness compares the parent measurement against that
    statement rather than against a count. `red_at_parent: true` says the case
    fails until the lane's outputs are written; `false` says it holds already
    and must keep holding.

    Optional, and deliberately: shipped plans in deployments carry no
    `declared_cases`, and a required field would refuse them at run start the
    way `RUN_PLAN_SCHEMA_VERSION_UNRUNNABLE` refuses a `maestro-plan.v1` plan.
    Absent means the lane is checked on `min_cases` alone, exactly as before.
    Present means every rule below binds -- a half-declared list is the shape
    that would let the check pass by naming one easy case.

    Not re-judged on a `bound_run`, for the same reason
    `OBLIGATION_UNOBSERVABLE` is not: an authoring obligation is judged when a
    plan is shipped, started or amended, never against a revision a run
    already holds.
    """
    if bound_run or lane_kind != "tests":
        return
    gate = spec.get("gate")
    if not isinstance(gate, Mapping):
        return
    declared = gate.get("declared_cases")
    if declared is None:
        return
    gate_ptr = pointer + "/spec/gate/declared_cases"
    if not isinstance(declared, list) or not declared:
        refusals.append(
            PlanRefusal(
                CASE_FALSIFICATION_UNDECLARED,
                gate_ptr,
                "declared_cases must be a nonempty array of "
                "{case, red_at_parent} objects",
            )
        )
        return
    names: List[str] = []
    reds = 0
    for index, item in enumerate(declared):
        item_ptr = gate_ptr + "/{0}".format(index)
        case = item.get("case") if isinstance(item, Mapping) else None
        red = item.get("red_at_parent") if isinstance(item, Mapping) else None
        if not isinstance(case, str) or not case.strip():
            refusals.append(
                PlanRefusal(
                    CASE_FALSIFICATION_UNDECLARED,
                    item_ptr + "/case",
                    "each declared case names the case identifier a runner "
                    "prints for it",
                )
            )
            continue
        if not isinstance(red, bool):
            refusals.append(
                PlanRefusal(
                    CASE_FALSIFICATION_UNDECLARED,
                    item_ptr + "/red_at_parent",
                    "case {0!r} must declare red_at_parent as a boolean; a "
                    "case whose expected outcome at the parent is unstated "
                    "cannot be falsified, only observed".format(case),
                )
            )
            continue
        if case.strip() in names:
            refusals.append(
                PlanRefusal(
                    CASE_FALSIFICATION_UNDECLARED,
                    item_ptr + "/case",
                    "duplicate declared case {0!r}".format(case),
                )
            )
            continue
        names.append(case.strip())
        reds += 1 if red else 0
    min_cases = gate.get("min_cases")
    floor = min_cases if isinstance(min_cases, int) and not isinstance(
        min_cases, bool
    ) else 1
    if len(names) < floor:
        refusals.append(
            PlanRefusal(
                CASE_FALSIFICATION_UNDECLARED,
                gate_ptr,
                "declared_cases names {0} case(s) against gate.min_cases {1}; "
                "a case the plan does not name has no declared outcome at the "
                "parent, so the suite's redness is a count again".format(
                    len(names), floor
                ),
            )
        )
    if names and reds == 0:
        refusals.append(
            PlanRefusal(
                CASE_FALSIFICATION_UNDECLARED,
                gate_ptr,
                "no declared case is red_at_parent; a suite that is green "
                "before the lane's outputs exist proves nothing about them",
            )
        )

def _validate_interface(
    pointer: str,
    spec: Mapping[str, Any],
    kinds: Mapping[str, Any],
    lane_id: str,
    needs: Sequence[Any],
    refusals: List[PlanRefusal],
    *,
    bound_run: bool = False,
) -> None:
    """A build lane paired with a tests lane must declare its public interface.

    The tester binds to the build lane's declared interface, not to names it
    invents: an undeclared binding is an unclosable review loop (the reviewer
    revises the invented name, the builder ships a different one, the suite
    keeps failing on a name that was never promised). ``spec.interface`` is
    the authored declaration; it is projected into ``public_contract`` so the
    tester, the test reviewer, the builder and the code reviewer all read the
    same bytes. Each entry also names its consumer (``consumed_by``); see
    ``_validate_consumers`` for why an unconsumed interface is a defect.

    The refusal applies exactly to a build lane paired with a tests lane:
    presence and shape are judged only there, so an interface-shaped value on
    a tests, untyped or unpaired lane is inert data, not a refusal. Like the
    obligation checks this is an authoring obligation: it is judged when a
    plan is shipped, started, or amended, and never re-judged against a
    revision a run already holds.
    """
    if bound_run:
        return
    if kinds.get(lane_id) != "build":
        return
    paired = any(
        isinstance(need, str) and kinds.get(need) == "tests" for need in needs
    )
    if not paired:
        return
    interface = spec.get("interface") if isinstance(spec, Mapping) else None
    for problem in interface_problems(interface):
        refusals.append(
            PlanRefusal(
                INTERFACE_UNDECLARED,
                pointer + "/spec/interface",
                problem,
            )
        )
    if isinstance(interface, list):
        _validate_consumers(pointer, interface, kinds, lane_id, refusals)
    if not interface:
        refusals.append(
            PlanRefusal(
                INTERFACE_UNDECLARED,
                pointer + "/spec/interface",
                "a build lane paired with a tests lane must declare the "
                "public interface its tests bind to; declare spec.interface "
                "entries naming each module, export and signature the suite "
                "may call",
            )
        )


def _validate_consumers(
    pointer: str,
    interface: Sequence[Any],
    kinds: Mapping[str, Any],
    lane_id: str,
    refusals: List[PlanRefusal],
) -> None:
    """Every declared interface entry names who will call it.

    FDAdb WP5 converged, published ``5ebb652c3037`` and shipped a module
    nothing calls: no producer, no mount, nothing importing
    ``RegulatorySection``. Every gate passed, because no gate ever asked who
    consumes the interface. The deferral was legitimate -- WP5b does the
    wiring -- but it was silent, so it was unreviewable.

    ``consumed_by`` is that answer, and it admits exactly two shapes:
    ``{"lane": "<lane-id>"}``, a lane of this plan whose declared outputs
    contain the call site, or ``{"deferred_to": "<work package>"}``, an
    explicit deferral naming the sibling work that will consume it. This is a
    declaration obligation the author answers, not a measurement: nothing
    here reads the repository or an import graph. A deferral is never refused
    for being a deferral -- it is refused only for being absent.

    A named lane may not be the paired tests lane. That lane is already in the
    build lane's ``needs``, so it is the cheapest string an author under review
    pressure writes -- and it certifies exactly the WP5 shape this check exists
    to refuse, because a tests lane's declared outputs are its accepted suite,
    never a call site. A lane with no declared outputs is refused earlier and
    harder: ``outputs`` must be a nonempty array (``SCHEMA_INVALID``), so no
    such lane reaches here in a plan that could otherwise compile.

    Judged exactly where ``_validate_interface`` judges presence and shape:
    on a build lane paired with a tests lane, at ship, start and amend only.
    """
    declared_lanes = set(kinds)
    for index, entry in enumerate(interface):
        entry_ptr = pointer + "/spec/interface/{0}/consumed_by".format(index)
        problems = consumer_problems(entry)
        if problems:
            for problem in problems:
                refusals.append(
                    PlanRefusal(INTERFACE_UNCONSUMED, entry_ptr, problem)
                )
            continue
        if not isinstance(entry, Mapping):
            continue
        consumer = entry.get("consumed_by")
        if not isinstance(consumer, Mapping) or "lane" not in consumer:
            continue
        named = str(consumer["lane"])
        if named == lane_id:
            refusals.append(
                PlanRefusal(
                    INTERFACE_UNCONSUMED,
                    entry_ptr,
                    "consumed_by.lane names the declaring lane itself; a lane "
                    "calling its own export is not a consumer",
                )
            )
        elif named not in declared_lanes:
            refusals.append(
                PlanRefusal(
                    INTERFACE_UNCONSUMED,
                    entry_ptr,
                    "consumed_by.lane must name a lane declared in this plan; "
                    "use deferred_to for work outside it",
                )
            )
        elif kinds.get(named) == "tests":
            refusals.append(
                PlanRefusal(
                    INTERFACE_UNCONSUMED,
                    entry_ptr,
                    "consumed_by.lane names a tests lane; a tests lane asserts "
                    "the interface, it does not consume it, and its declared "
                    "outputs are the accepted suite rather than a call site. "
                    "Name the lane that calls this export, or declare "
                    "deferred_to",
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


def _validate_test_suite_outputs(
    parsed: Sequence[Tuple[int, str, Sequence[Any], Sequence[Any], Any, Sequence[Any]]],
    kinds: Mapping[str, str | None],
    refusals: List[PlanRefusal],
) -> None:
    """No lane may declare an output covering another lane's test suite.

    A ``lane_kind=tests`` lane's declared outputs ARE its accepted suite: the
    tester authors its private acceptance files exactly at those paths
    (``MAESTRO_architecture.md`` §11). So a build lane that also declares one of
    them is asking to own the bytes it is graded against, and §11 recorded that
    neither this validator nor the compiler forbade it -- the candidate was kept
    off those paths only by a pathspec subtraction at commit time, one runtime
    step with nothing upstream of it.

    ``_validate_ownership`` already refuses the same plans under
    ``OUTPUT_OWNERSHIP_CONFLICT``, and keeps doing so; this adds a refusal
    rather than replacing one. What the generic rule cannot say is WHICH
    invariant broke. "Two lanes want to own this path" is an authoring mistake
    either lane could fix by renaming; "a lane wants to own its own grader" is
    not, and the two stop being the same sentence the moment the accepted suite
    is carried in the builder's checkout, which it is.

    Untyped lanes are covered on the same terms. Their own hidden meta-tests sit
    at paths of the tester's choosing and are not decidable here, but a typed
    tests lane's outputs are in the plan, and an untyped lane may not claim them.
    """
    suite: dict[str, str] = {}
    for _index, lane_id, _needs, outputs, _spec, _acceptance in parsed:
        if kinds.get(lane_id) != "tests":
            continue
        for raw in outputs:
            normalized = normalize_declared_output(raw)
            if normalized is not None:
                suite.setdefault(normalized, lane_id)
    if not suite:
        return
    for index, lane_id, _needs, outputs, _spec, _acceptance in parsed:
        if kinds.get(lane_id) == "tests":
            continue
        for out_index, raw in enumerate(outputs):
            normalized = normalize_declared_output(raw)
            if normalized is None:
                continue
            for path, tests_lane in sorted(suite.items()):
                if not outputs_conflict(normalized, path):
                    continue
                refusals.append(
                    PlanRefusal(
                        OUTPUT_OVERLAPS_TEST_SUITE,
                        "/lanes/{0}/outputs/{1}".format(index, out_index),
                        "lane {0} declares {1}, which covers {2}, the accepted "
                        "test suite of lane {3}; a lane may not own the bytes "
                        "it is graded against".format(
                            lane_id, normalized, path, tests_lane
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
