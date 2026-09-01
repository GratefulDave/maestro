"""An unconsumed tests lane gates the run, not a lane.

Regression for FDAdb run f50638ab..., where lane-wp7-e2e-tests was a root and a
leaf: its 6 sealed cases read files owned by both build lanes, no build lane
consumed it, and nothing was ever obliged to make it green. The plan compiler
requires every build lane to have a tests dependency but never checked the
converse, so the obligation existed only in two acceptance strings.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st


def lane(
    lane_id: str,
    *,
    kind: str,
    needs: tuple[str, ...] = (),
) -> st.LaneProjection:
    spec_digest = st.digest_canonical({"spec": lane_id})
    needs = tuple(sorted(needs))
    outputs = (f"{lane_id}.py",)
    return st.LaneProjection(
        lane_id=lane_id,
        needs=needs,
        spec_digest=spec_digest,
        declared_outputs=outputs,
        lane_projection_digest=st.lane_projection_digest(
            spec_digest, needs, outputs, lane_kind=kind
        ),
        public_acceptance=("observable behavior",),
        lane_kind=kind,
    )


def gate_lanes(*lanes: st.LaneProjection) -> tuple[str, ...]:
    scheduler = object.__new__(sch.FactoryScheduler)
    return tuple(
        item.lane_id
        for item in sch.FactoryScheduler._run_gate_lanes(scheduler, lanes)
    )


# The exact WP7 shape: two paired chains plus one unconsumed cross-cutting suite.
WP7 = (
    lane("lane-wp7-tests", kind="tests"),
    lane("lane-wp7-gateway-tests", kind="tests"),
    lane("lane-wp7-e2e-tests", kind="tests"),
    lane("lane-wp7-build", kind="build", needs=("lane-wp7-tests",)),
    lane(
        "lane-wp7-gateway-build",
        kind="build",
        needs=("lane-wp7-gateway-tests",),
    ),
)


def test_unconsumed_tests_lane_is_a_run_gate() -> None:
    assert gate_lanes(*WP7) == ("lane-wp7-e2e-tests",)


def test_consumed_tests_lanes_are_not_run_gates() -> None:
    selected = gate_lanes(*WP7)
    assert "lane-wp7-tests" not in selected
    assert "lane-wp7-gateway-tests" not in selected


def test_build_lanes_are_never_run_gates() -> None:
    selected = gate_lanes(*WP7)
    assert "lane-wp7-build" not in selected
    assert "lane-wp7-gateway-build" not in selected


def test_a_fully_paired_plan_has_no_run_gate() -> None:
    paired = (
        lane("t", kind="tests"),
        lane("b", kind="build", needs=("t",)),
    )
    assert gate_lanes(*paired) == ()


def test_a_tests_lane_consumed_by_any_build_lane_is_not_a_gate() -> None:
    # Two builds sharing one tests lane still consume it.
    shared = (
        lane("t", kind="tests"),
        lane("b1", kind="build", needs=("t",)),
        lane("b2", kind="build", needs=("t",)),
    )
    assert gate_lanes(*shared) == ()


def test_a_tests_lane_needed_only_by_another_tests_lane_still_gates() -> None:
    # Only a build lane consumes a suite. A tests->tests edge does not discharge it.
    lanes = (
        lane("t-inner", kind="tests"),
        lane("t-outer", kind="tests", needs=("t-inner",)),
        lane("b", kind="build", needs=("t-outer",)),
    )
    assert gate_lanes(*lanes) == ("t-inner",)
