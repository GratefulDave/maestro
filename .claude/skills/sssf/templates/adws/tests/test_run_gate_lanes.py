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

_REAL_RUN_ROW = sch.run_row


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


def test_a_red_run_gate_names_the_lane_whose_suite_failed() -> None:
    """A REVISE must name someone; a red gate is evidence about one lane only.

    The blame used to be every build lane in the plan, whichever suite went
    red. On run f50638ab that named `lane-wp7-gateway-build` over an assertion
    about a browser route it does not own -- and since an amendment must change
    the spec of every lane a review names, correcting one bad test assertion
    could only be done by also editing two builds that were already right.

    Naming is a floor, not a ceiling: the operator may still amend any other
    lane in the same amendment. What the harness must not do is *require* an
    edit it has no evidence for.
    """
    head = "a" * 40
    fingerprint = "b" * 64
    recorded: dict[str, object] = {}

    class _Store:
        def active_final_review_fingerprint(self, run_id: str, integration: str) -> str:
            del run_id, integration
            return fingerprint

        def active_projection(self, run_id: str) -> tuple[st.LaneProjection, ...]:
            del run_id
            return WP7

        def complete_final_review(
            self, run_id, review_fingerprint, integration, observed, artifact, affected
        ):
            del run_id, review_fingerprint, integration, observed
            recorded["payload"] = artifact.payload
            recorded["affected"] = tuple(affected)
            return None

    class _Locks:
        def acquire(self, level: int) -> None:
            del level

        def release(self) -> None:
            return None

    class _Git:
        def rev_parse(self, ref: str) -> str:
            del ref
            return "c" * 40

    class _Target:
        target_repository_root = "/nonexistent"

        def git(self) -> _Git:
            return _Git()

    class _Actor:
        called = False

        def review_integration(self, ctx, lanes, integration):
            del ctx, lanes, integration
            _Actor.called = True
            raise AssertionError("the reviewer must not run when a gate is red")

    scheduler = object.__new__(sch.FactoryScheduler)
    scheduler.run_id = "run-1"
    scheduler.store = _Store()
    scheduler.locks = _Locks()
    scheduler.target = _Target()
    scheduler.actor = _Actor()
    scheduler._compiled = None
    scheduler._integration_head = lambda: head
    scheduler._plan_artifact_ref = lambda row: "plan.v1"
    scheduler._failed_run_gates = lambda lanes, h, f: ("lane-wp7-e2e-tests",)
    sch.run_row = lambda store, run_id: {  # type: ignore[assignment]
        "plan_revision": 1,
        "plan_digest": "d" * 64,
        "target_main_ref": "refs/heads/main",
    }

    try:
        sch.FactoryScheduler._final_review(scheduler)
    finally:
        sch.run_row = _REAL_RUN_ROW  # type: ignore[assignment]

    payload = recorded["payload"]
    assert payload["verdict"] == st.ReviewerVerdict.REVISE.value
    assert recorded["affected"] == ("lane-wp7-e2e-tests",)
    assert payload["affected_lanes"] == ["lane-wp7-e2e-tests"]

    # The lanes that used to be blamed, and are not the ones with evidence.
    build_lanes = {
        item.lane_id for item in WP7 if item.lane_kind == st.LANE_KIND_BUILD
    }
    assert build_lanes == {"lane-wp7-build", "lane-wp7-gateway-build"}
    assert not build_lanes.intersection(recorded["affected"])
    assert _Actor.called is False
