"""The two records `run attend` leaves, written into a real ledger.

A CHECK constraint is baked into a table at creation, so these cases are the
only ones that would have caught the failure mode the v3 migration exists for:
an artifact kind the enum admits and the ledger refuses. They are written
against `ArtifactStore` rather than a fake for exactly that reason.

They also pin what the records are *not*. Neither kind appears in
`COMPLETE_STAGE_EDGES`, neither touches `lane_state`, and writing either one
twice for the same session or the same amendment replays the existing row
rather than appending a second -- because a record of why a decision was made
must never become a second way of making one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402
from tests.test_artifact_store import (  # noqa: E402
    RUN_ID,
    make_binding,
    make_lane,
    make_plan,
)


class AttendRecords(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.store = ArtifactStore(Path(holder.name) / "lifecycle.sqlite3")
        self.addCleanup(self.store.close)
        self.lane_a = make_lane("A")
        self.plan = make_plan(self.lane_a, make_lane("B", needs=("A",), outputs=("b.py",)))
        self.store.create_run(RUN_ID, self.plan, make_binding())

    def _stages(self) -> dict:
        return {
            lane.lane_id: self.store.lane_stage(RUN_ID, lane.lane_id)
            for lane in self.store.active_projection(RUN_ID)
        }

    def test_a_session_records_its_start_and_its_stop(self) -> None:
        before = self._stages()
        self.store.record_attend_session(
            RUN_ID,
            session_id="s-1",
            phase=st.ATTEND_PHASE_START,
            payload={"started_at": "2026-09-10T00:00:00Z"},
        )
        self.store.record_attend_session(
            RUN_ID,
            session_id="s-1",
            phase=st.ATTEND_PHASE_STOP,
            payload={
                "stopped_at": "2026-09-10T00:40:00Z",
                "stop_reason": "RUN_SETTLED",
                "revisions_applied": 2,
                "lanes_amended": ["A"],
            },
        )
        rows = self.store.run_artifacts_of_kind(RUN_ID, st.ArtifactKind.ATTEND_SESSION)
        self.assertEqual([row["payload"]["phase"] for row in rows], ["START", "STOP"])
        self.assertEqual(rows[1]["payload"]["stop_reason"], "RUN_SETTLED")
        self.assertEqual(rows[1]["payload"]["revisions_applied"], 2)
        # Recorded, and inert.
        self.assertEqual(self._stages(), before)

    def test_a_second_attend_session_on_the_same_run_is_its_own_pair(self) -> None:
        for session in ("s-1", "s-2"):
            for phase in (st.ATTEND_PHASE_START, st.ATTEND_PHASE_STOP):
                self.store.record_attend_session(
                    RUN_ID, session_id=session, phase=phase, payload={}
                )
        rows = self.store.run_artifacts_of_kind(RUN_ID, st.ArtifactKind.ATTEND_SESSION)
        self.assertEqual(
            [(row["payload"]["session_id"], row["payload"]["phase"]) for row in rows],
            [("s-1", "START"), ("s-1", "STOP"), ("s-2", "START"), ("s-2", "STOP")],
        )

    def test_replaying_one_end_of_a_session_appends_nothing(self) -> None:
        first = self.store.record_attend_session(
            RUN_ID, session_id="s-1", phase=st.ATTEND_PHASE_START, payload={"a": 1}
        )
        again = self.store.record_attend_session(
            RUN_ID, session_id="s-1", phase=st.ATTEND_PHASE_START, payload={"a": 1}
        )
        self.assertEqual(again.artifact_id, first.artifact_id)
        self.assertTrue(again.replayed)
        self.assertEqual(
            len(self.store.run_artifacts_of_kind(RUN_ID, st.ArtifactKind.ATTEND_SESSION)),
            1,
        )

    def test_a_rationale_is_bound_to_the_amendment_it_explains(self) -> None:
        record = self.store.record_amendment_rationale(
            RUN_ID,
            lane_id="A",
            amendment_artifact_id="amendment-xyz",
            payload={
                "attend_session_id": "s-1",
                "rationale": {
                    "lane": "A",
                    "round": 4,
                    "failing_cases_summary": "producer omits the disclaimer",
                    "contract_gap": "seam never required disclaimer",
                    "edit_path": "seams[2].contract",
                    "edit_text": "the producer emits `disclaimer`",
                },
            },
        )
        rows = self.store.run_artifacts_of_kind(
            RUN_ID, st.ArtifactKind.AMENDMENT_RATIONALE
        )
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload"]
        self.assertEqual(payload["lane_id"], "A")
        self.assertEqual(payload["amendment_artifact_id"], "amendment-xyz")
        self.assertEqual(
            payload["rationale"]["contract_gap"], "seam never required disclaimer"
        )
        self.assertFalse(record.replayed)

    def test_a_rationale_for_a_different_amendment_is_a_different_record(self) -> None:
        for amendment in ("amendment-1", "amendment-2"):
            self.store.record_amendment_rationale(
                RUN_ID,
                lane_id="A",
                amendment_artifact_id=amendment,
                payload={"rationale": {}},
            )
        rows = self.store.run_artifacts_of_kind(
            RUN_ID, st.ArtifactKind.AMENDMENT_RATIONALE
        )
        self.assertEqual(
            [row["payload"]["amendment_artifact_id"] for row in rows],
            ["amendment-1", "amendment-2"],
        )

    def test_neither_kind_is_a_stage_edge(self) -> None:
        edges = {edge.kind for edge in st.COMPLETE_STAGE_EDGES}
        self.assertNotIn(st.ArtifactKind.ATTEND_SESSION, edges)
        self.assertNotIn(st.ArtifactKind.AMENDMENT_RATIONALE, edges)
        self.assertNotIn(st.ArtifactKind.ATTEND_SESSION, st.LANE_ARTIFACT_KINDS)
        self.assertNotIn(st.ArtifactKind.AMENDMENT_RATIONALE, st.LANE_ARTIFACT_KINDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
