"""A carried lane's CODE_REVISE resolves the candidate its review judged.

Run 2acd2f5f, plan revision 8:

    FACTORY_REFUSED:CODE_REVISE missing prior artifacts

`lane-spend-build` sat in a resumable wait when attend amended a different
lane. `amendment_reset_stage` keeps a lane in a resumable wait where it is, so
the lane was carried, not reset, and resumed into REVIEWING_CODE on the
BUILDER_OUTPUT it had built under revision 7. The reviewer answered REVISE,
stamped revision 8. BUILDING then asked for the newest BUILDER_OUTPUT *at
revision 8* and got nothing, with the candidate present one row up.

The review names its candidate in `input_artifact_ids`. Re-deriving it by
recency under a revision filter is a second answer to a question the record
already answered, and across a revision boundary the answers differ -- the
same shape as `_base_invalidation_priors`.

The sibling: a suite-owning lane carried the same way (a tests lane resumed
at READY_TO_MERGE, an untyped lane resumed at BUILDING or REVIEWING_CODE)
found no ACCEPTED_TEST_SUITE at the live revision, because only a MERGED lane
fell back to the suite it sealed.

Both the scheduler (which writes the BUILDING digest) and the ledger (which
reconstructs it) are exercised against a real sqlite ledger holding the run's
shape.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402

RUN = "2acd2f5fb31f4af6a7acb4b799ddb05d"
LANE = "lane-spend-build"
TESTS = "lane-spend-tests"

PLAN_ID = "1" * 64
SUITE_ID = "2" * 64
BUILDER_ID = "6" * 64
REVISE_ID = "d" * 64


class _Ledger:
    """A sqlite ledger with the columns the lookups read, and the store's own methods."""

    _run = ArtifactStore._run
    lane_stage = ArtifactStore.lane_stage
    _lane_artifact_by_id = ArtifactStore._lane_artifact_by_id
    _latest_lane_artifact = ArtifactStore._latest_lane_artifact
    _revise_prior = ArtifactStore._revise_prior
    _current_accepted_suite = ArtifactStore._current_accepted_suite

    def __init__(self, revision: int) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE runs(run_id TEXT, plan_revision INTEGER);"
            "CREATE TABLE lane_state(run_id TEXT, lane_id TEXT, stage TEXT);"
            "CREATE TABLE lane_artifacts(artifact_id TEXT, run_id TEXT, lane_id TEXT,"
            " sequence INTEGER, artifact_kind TEXT, plan_revision INTEGER,"
            " input_digest TEXT, output_digest TEXT, artifact_ref TEXT,"
            " payload_json TEXT, lane_projection_digest TEXT);"
        )
        self.conn.execute("INSERT INTO runs VALUES (?,?)", (RUN, revision))
        self.sequence = 0

    def stage(self, lane_id: str, stage: st.LaneStage) -> None:
        self.conn.execute("DELETE FROM lane_state WHERE lane_id=?", (lane_id,))
        self.conn.execute(
            "INSERT INTO lane_state VALUES (?,?,?)", (RUN, lane_id, stage.value)
        )

    def add(self, lane_id, artifact_id, kind, revision, payload) -> None:
        self.sequence += 1
        self.conn.execute(
            "INSERT INTO lane_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                RUN,
                lane_id,
                self.sequence,
                kind.value,
                revision,
                "i" * 64,
                "o" * 64,
                "ref",
                json.dumps(payload),
                "p" * 64,
            ),
        )


def _carried_build_lane() -> _Ledger:
    """BUILDER_OUTPUT at revision 7; the REVISE that judged it at revision 8."""
    ledger = _Ledger(revision=8)
    ledger.stage(LANE, st.LaneStage.BUILDING)
    ledger.add(
        LANE,
        BUILDER_ID,
        st.ArtifactKind.BUILDER_OUTPUT,
        7,
        {"candidate_sha": "f" * 40, "builder_base_sha": "a" * 40},
    )
    ledger.add(
        LANE,
        REVISE_ID,
        st.ArtifactKind.CODE_REVIEW,
        8,
        {"verdict": "REVISE", "input_artifact_ids": [PLAN_ID, SUITE_ID, BUILDER_ID]},
    )
    return ledger


def _revise(ledger: _Ledger) -> sch.ArtifactRecord:
    record = sch._latest(
        ledger,
        RUN,
        LANE,
        st.ArtifactKind.CODE_REVIEW,
        verdict=st.ReviewerVerdict.REVISE,
        plan_revision=8,
    )
    assert record is not None
    return record


def test_the_revision_scoped_lookup_is_what_refused() -> None:
    """The run's shape, reproduced: nothing at revision 8, the candidate at 7."""
    ledger = _carried_build_lane()
    assert (
        sch._latest(
            ledger, RUN, LANE, st.ArtifactKind.BUILDER_OUTPUT, plan_revision=8
        )
        is None
    )


def test_scheduler_resolves_the_candidate_the_review_named() -> None:
    ledger = _carried_build_lane()
    prior = sch._revise_prior(ledger, RUN, LANE, _revise(ledger))
    assert prior is not None, "CODE_REVISE missing prior artifacts"
    assert prior.artifact_id == BUILDER_ID
    assert prior.plan_revision == 7


def test_ledger_reconstructs_the_same_candidate() -> None:
    """The ledger re-derives the scheduler's digest; both must pick one artifact."""
    ledger = _carried_build_lane()
    review = ledger._latest_lane_artifact(
        RUN, LANE, st.ArtifactKind.CODE_REVIEW, verdict=st.ReviewerVerdict.REVISE
    )
    prior = ledger._revise_prior(RUN, LANE, review)
    assert prior is not None and prior["artifact_id"] == BUILDER_ID


def test_a_newer_unreviewed_builder_output_is_not_the_prior() -> None:
    """Recency and the record disagree; the record wins in both readers."""
    ledger = _carried_build_lane()
    ledger.add(LANE, "9" * 64, st.ArtifactKind.BUILDER_OUTPUT, 8, {})
    assert sch._revise_prior(ledger, RUN, LANE, _revise(ledger)).artifact_id == BUILDER_ID
    review = ledger._latest_lane_artifact(
        RUN, LANE, st.ArtifactKind.CODE_REVIEW, verdict=st.ReviewerVerdict.REVISE
    )
    assert ledger._revise_prior(RUN, LANE, review)["artifact_id"] == BUILDER_ID


def test_a_review_that_names_no_candidate_refuses_rather_than_guessing() -> None:
    ledger = _carried_build_lane()
    ledger.add(
        LANE, "e" * 64, st.ArtifactKind.CODE_REVIEW, 8, {"verdict": "REVISE"}
    )
    revise = _revise(ledger)
    assert revise.artifact_id == "e" * 64
    assert sch._revise_prior(ledger, RUN, LANE, revise) is None
    assert sch._revise_prior(ledger, RUN, LANE, None) is None


def _carried_tests_lane(stage: st.LaneStage) -> _Ledger:
    ledger = _Ledger(revision=8)
    ledger.stage(TESTS, stage)
    ledger.add(
        TESTS,
        SUITE_ID,
        st.ArtifactKind.ACCEPTED_TEST_SUITE,
        7,
        {"candidate_sha": "c" * 40, "builder_base_sha": "a" * 40},
    )
    ledger.add(
        TESTS,
        "t" * 64,
        st.ArtifactKind.TEST_REVIEW,
        7,
        {"verdict": "PASS"},
    )
    return ledger


def _scheduler(ledger: _Ledger) -> SimpleNamespace:
    return SimpleNamespace(store=ledger, run_id=RUN)


def test_a_carried_suite_owning_lane_keeps_the_suite_it_sealed() -> None:
    for stage in st.SUITE_HOLDING_STAGES:
        ledger = _carried_tests_lane(stage)
        found = sch.FactoryScheduler._current_accepted_suite(_scheduler(ledger), TESTS)
        assert found is not None and found.artifact_id == SUITE_ID, stage
        row = ledger._current_accepted_suite(RUN, TESTS)
        assert row is not None and row["artifact_id"] == SUITE_ID, stage


def test_a_carried_tests_lane_at_ready_to_merge_has_its_merge_producers() -> None:
    ledger = _carried_tests_lane(st.LaneStage.READY_TO_MERGE)
    lane = SimpleNamespace(lane_id=TESTS, lane_kind=st.LANE_KIND_TESTS)
    suite, review = sch._merge_producers(ledger, RUN, lane)
    assert suite is not None and suite.artifact_id == SUITE_ID
    assert review is not None


def test_a_reset_lane_does_not_reach_back_for_an_old_suite() -> None:
    """A reset suite-owning lane is at TESTS_SEALED or earlier and seals again."""
    for stage in st.UNSTARTED_STAGES:
        ledger = _carried_tests_lane(stage)
        assert (
            sch.FactoryScheduler._current_accepted_suite(_scheduler(ledger), TESTS)
            is None
        ), stage
        assert ledger._current_accepted_suite(RUN, TESTS) is None, stage


def test_amendment_resets_suite_owning_lanes_below_the_holding_stages() -> None:
    """The invariant `SUITE_HOLDING_STAGES` rests on, read off the reset policy."""
    for kind in (st.LANE_KIND_TESTS, None):
        for current in st.SUITE_HOLDING_STAGES:
            for changed in (True, False):
                target = st.amendment_reset_stage(
                    current,
                    changed=changed,
                    wait_reason=None,
                    lane_kind=kind,
                    unaffected=False,
                )
                assert target not in st.SUITE_HOLDING_STAGES, (kind, current, changed)


def test_neither_scheduler_site_derives_the_prior_by_recency() -> None:
    source = (RUNTIME_ROOT / "adw_modules" / "scheduler.py").read_text()
    bodies = [
        block.split('raise FactoryRefused("CODE_REVISE missing prior artifacts")')[0]
        for block in source.split("if entry is st.BuildingEntryKind.CODE_REVISE:")[1:]
        if 'raise FactoryRefused("CODE_REVISE missing prior artifacts")' in block
    ]
    assert len(bodies) == 2, "both scheduler sites must be covered"
    for body in bodies:
        assert "_revise_prior(" in body
        assert "BUILDER_OUTPUT" not in body
