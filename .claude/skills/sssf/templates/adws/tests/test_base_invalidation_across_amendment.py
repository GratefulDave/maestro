"""A base invalidation resolves the candidate it invalidated, not the newest one.

Run c9e5b420, plan revision 4:

    FACTORY_REFUSED:BASE_INVALIDATION missing artifacts

`lane-wp4-clearances-build` was carried across an amendment that did not touch
it, re-merged, found the integration head had moved, recorded a
BASE_INVALIDATION, and then could not compute its own BUILDING input digest.
Its BUILDER_OUTPUT and its passing CODE_REVIEW are real and present -- at plan
revision 3. The lookups asked for the newest of each kind *at revision 4* and
got nothing.

The invalidation record already names both artifacts, in
`stale_builder_output_artifact_id` and `stale_code_review_artifact_id`. Asking
recency for an answer the record already carries is the defect; across a
revision boundary the two answers differ, and inside one revision they happen
to agree, which is why this went unseen until a merged lane began re-entering
BUILDING through READY_TO_MERGE instead of as INITIAL.

Both readers are covered here. The scheduler writes the digest and the ledger
reconstructs it, so they have to resolve the same two artifacts or every
verification of that stage fails.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

RUN = "c9e5b42013a84883b5ce260b6b69c577"
LANE = "lane-wp4-clearances-build"

BUILDER_ID = "b" * 64
REVIEW_ID = "r" * 64


class _Rows(dict):
    """Just enough of sqlite3.Row for the by-id lookup under test."""

    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _row(artifact_id, kind, revision, payload=None):
    return _Rows(
        artifact_id=artifact_id,
        run_id=RUN,
        lane_id=LANE,
        sequence=1,
        artifact_kind=kind.value,
        plan_revision=revision,
        input_digest="d" * 64,
        output_digest="e" * 64,
        artifact_ref="ref",
        payload_json="{}" if payload is None else __import__("json").dumps(payload),
    )


class _Store:
    """A ledger holding the run's real shape: priors at 3, invalidation at 4."""

    def __init__(self) -> None:
        self.rows = {
            BUILDER_ID: _row(
                BUILDER_ID,
                st.ArtifactKind.BUILDER_OUTPUT,
                3,
                {"builder_base_sha": "a" * 40, "candidate_sha": "c" * 40},
            ),
            REVIEW_ID: _row(REVIEW_ID, st.ArtifactKind.CODE_REVIEW, 3, {"verdict": "PASS"}),
        }
        self.queried_ids: list[str] = []
        self.conn = SimpleNamespace(execute=self._execute)

    def _execute(self, sql, params):
        assert "artifact_id=?" in sql, "the lookup must be by id, not by recency"
        artifact_id = params[-1]
        self.queried_ids.append(artifact_id)
        return SimpleNamespace(fetchone=lambda: self.rows.get(artifact_id))


def _invalidation() -> sch.ArtifactRecord:
    return sch.ArtifactRecord(
        artifact_id="i" * 64,
        run_id=RUN,
        lane_id=LANE,
        sequence=11,
        kind=st.ArtifactKind.BASE_INVALIDATION,
        plan_revision=4,
        input_digest="d" * 64,
        output_digest="e" * 64,
        artifact_ref="ref",
        payload={
            "stale_builder_output_artifact_id": BUILDER_ID,
            "stale_code_review_artifact_id": REVIEW_ID,
            "stale_builder_base_sha": "a" * 40,
            "stale_candidate_sha": "c" * 40,
            "integration_head": "f" * 40,
        },
    )


def test_priors_resolve_across_a_revision_boundary() -> None:
    """The refusal that stopped run c9e5b420, reproduced and answered."""
    store = _Store()
    prior, passing = sch._base_invalidation_priors(store, RUN, LANE, _invalidation())

    assert prior is not None, "BASE_INVALIDATION missing artifacts"
    assert passing is not None, "BASE_INVALIDATION missing artifacts"
    assert prior.artifact_id == BUILDER_ID
    assert passing.artifact_id == REVIEW_ID
    # And they came from the record, not from a search.
    assert store.queried_ids == [BUILDER_ID, REVIEW_ID]


def test_the_priors_kept_their_own_revision() -> None:
    """Revision 3 artifacts under a revision 4 invalidation: the whole point."""
    store = _Store()
    prior, passing = sch._base_invalidation_priors(store, RUN, LANE, _invalidation())
    assert prior.plan_revision == 3 and passing.plan_revision == 3


def test_no_invalidation_yields_no_priors() -> None:
    store = _Store()
    assert sch._base_invalidation_priors(store, RUN, LANE, None) == (None, None)
    assert store.queried_ids == []


def test_a_malformed_invalidation_refuses_rather_than_guessing() -> None:
    """A record that names nothing must not fall back to a recency search."""
    store = _Store()
    broken = _invalidation()
    object.__setattr__(broken, "payload", {"integration_head": "f" * 40})
    assert sch._base_invalidation_priors(store, RUN, LANE, broken) == (None, None)
    assert store.queried_ids == []


def test_both_readers_use_the_stored_ids() -> None:
    """The scheduler writes the digest and the ledger reconstructs it.

    If one resolves by id and the other by recency they agree inside a
    revision and diverge across one, which is a verification that passes
    until exactly the case that matters.
    """
    scheduler_src = (RUNTIME_ROOT / "adw_modules" / "scheduler.py").read_text()
    lifecycle_src = (RUNTIME_ROOT / "adw_modules" / "lifecycle.py").read_text()
    for source in (scheduler_src, lifecycle_src):
        assert "stale_builder_output_artifact_id" in source
        assert "stale_code_review_artifact_id" in source

    # And neither still reaches for a recency search inside the branch body:
    # from the entry-kind test to the refusal that closes it.
    bodies = [
        block.split('raise FactoryRefused("BASE_INVALIDATION missing artifacts")')[0]
        for block in scheduler_src.split(
            "elif entry is st.BuildingEntryKind.BASE_INVALIDATION:"
        )[1:]
    ]
    assert len(bodies) == 2, "both scheduler sites must be covered"
    for body in bodies:
        assert "plan_revision=revision" not in body, (
            "a base invalidation must not re-derive its priors by recency"
        )
        assert "_base_invalidation_priors" in body

    ledger_body = lifecycle_src.split(
        'raise StaleStageInput("BASE_INVALIDATION variant missing artifacts")'
    )[0][-1400:]
    assert "stale_builder_output_artifact_id" in ledger_body
    assert "_lane_artifact_by_id" in ledger_body
