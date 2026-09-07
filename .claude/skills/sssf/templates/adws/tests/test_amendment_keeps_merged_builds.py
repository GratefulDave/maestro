"""An amendment does not rebuild a merged build lane it did not change.

An amendment replays the integration ref, so a merged build lane has to merge
again. It does not have to *build* again: its candidate, its builder base and
its PASS review are exactly the inputs READY_TO_MERGE consumes, and
`decide_merge_action` is already the reader that asks whether the base moved.

`amendment_reset_stage` used to return BUILDING for every merged build lane. It
reads `changed` once, at the top, and then never again, so the answer did not
depend on whether the amendment touched the lane. On run c9e5b420 that cost:

    amendment r9  (touched lane-wp4-release-tests only)
        lane-wp4-clearances-build MERGED -> BUILDING
    amendment r10 (touched lane-wp4-recalls-build only)
        lane-wp4-clearances-build MERGED -> BUILDING
        lane-wp4-release-build    MERGED -> BUILDING

`lane-wp4-clearances-build` was built and code-reviewed three times, once per
revision, and no amendment ever named it. `lane-wp4-release-build` passed review
at revision 2, was rebuilt untouched at revision 3, and its rebuild introduced
two defects in code that had been correct. With three build lanes every
amendment re-rolled all three, so fixing one lane re-ran the others and could
break them: the run could not converge.

The tests-lane arm is deliberately left alone. An unchanged merged tests lane
reseals, which runs no agent -- that arm was correct when a6f019b introduced
both.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from adw_modules import scheduler_types as st  # noqa: E402

BUILD = st.LANE_KIND_BUILD
TESTS = st.LANE_KIND_TESTS if hasattr(st, "LANE_KIND_TESTS") else "tests"


def _reset(current, *, changed, wait_reason=None, kind=BUILD):
    return st.amendment_reset_stage(
        current, changed=changed, wait_reason=wait_reason, lane_kind=kind
    )


def test_an_untouched_merged_build_lane_keeps_its_build() -> None:
    """The whole point: no builder round, no reviewer round, merge only."""
    assert _reset(st.LaneStage.MERGED, changed=False) is st.LaneStage.READY_TO_MERGE


def test_a_changed_merged_build_lane_still_starts_over() -> None:
    """An amendment that names the lane is asking for a new candidate."""
    assert _reset(st.LaneStage.MERGED, changed=True) is st.LaneStage.PLANNED


@pytest.mark.parametrize(
    "stage",
    [
        st.LaneStage.BUILDING,
        st.LaneStage.REVIEWING_CODE,
        st.LaneStage.READY_TO_MERGE,
    ],
)
def test_a_build_lane_in_flight_is_unaffected(stage) -> None:
    """Only the MERGED arm moved; a lane mid-implementation still rewinds."""
    assert _reset(stage, changed=False) is st.LaneStage.BUILDING


def test_an_untouched_merged_tests_lane_still_reseals() -> None:
    """The sibling arm, deliberately unchanged: resealing runs no agent."""
    assert (
        _reset(st.LaneStage.MERGED, changed=False, kind=TESTS)
        is st.LaneStage.TESTS_SEALED
    )


def test_ready_to_merge_consumes_exactly_what_a_merged_lane_already_has() -> None:
    """Why READY_TO_MERGE is a legal target and BUILDING was overkill.

    The stage's own input digest is built from a BUILDER_OUTPUT and a PASS
    CODE_REVIEW. A merged lane has both by definition -- it could not have
    merged otherwise -- so resetting into that stage asks for nothing the
    ledger does not already hold.
    """
    source = (RUNTIME_ROOT / "adw_modules" / "scheduler.py").read_text()
    guards = source.split('raise FactoryRefused("missing READY_TO_MERGE inputs")')
    assert len(guards) > 1, "the stage no longer states its own inputs"
    # Every place that guards the stage looks up the same two artifacts first.
    for preceding in guards[:-1]:
        window = preceding[-900:]
        assert "ArtifactKind.BUILDER_OUTPUT" in window
        assert "ReviewerVerdict.PASS" in window
    # And it is a stage the scheduler can be resumed into at all.
    assert st.LaneStage.READY_TO_MERGE in st.PAUSEABLE_STAGES


def test_a_moved_base_is_still_caught_and_still_rebuilds() -> None:
    """The question BUILDING was answering is answered by the merge decision.

    A lane that keeps its build and then finds the integration head is not the
    base it built against takes BASE_INVALIDATION, which is what returns it to
    BUILDING. That path is unchanged, so this is a narrowing of when a rebuild
    happens, not a removal of it.
    """
    from adw_modules import git_publication as gitpub

    moved = gitpub.decide_merge_action(
        changed=False,
        builder_base_sha="a" * 40,
        candidate_sha="a" * 40,
        integration_head="b" * 40,
    )
    assert moved.action == "BASE_INVALIDATION"

    still = gitpub.decide_merge_action(
        changed=False,
        builder_base_sha="a" * 40,
        candidate_sha="a" * 40,
        integration_head="a" * 40,
        sealed_present=True,
    )
    assert still.action != "BASE_INVALIDATION"


def test_the_run_c9e5b420_reset_table_would_not_have_rebuilt_those_lanes() -> None:
    """The two amendments that cost this run, replayed against the new rule."""
    # r9 touched lane-wp4-release-tests only; r10 touched lane-wp4-recalls-build
    # only. Every other build lane was MERGED and unchanged.
    for _amendment in ("r9", "r10"):
        assert (
            _reset(st.LaneStage.MERGED, changed=False) is not st.LaneStage.BUILDING
        ), "an untouched merged build lane must not be sent back to the builder"
