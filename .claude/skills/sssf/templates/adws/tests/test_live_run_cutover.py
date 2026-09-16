"""A run holding a pre-v5 suite artifact refuses by name; it does not crash.

The v4 -> v5 migration keeps `SEALED_TEST_BUNDLE` and `TEST_INVALIDATION`
rows readable, but `ArtifactKind` no longer has either member, so any reader
that turns a stored kind string into the enum raised a bare `ValueError` on
`run resume` and `run status`. Every verb that binds a run now answers
`LIVE_RUN_CUTOVER_REQUIRED:<run_id>:<kind>` through the operator refusal path,
and a ledger with neither kind binds exactly as before.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from tests.test_run_status import _init_repo, _install_deployment, _outcome, _plan_bytes


class _Launched(Exception):
    """Raised in place of an actor: the verb got past binding."""


class LiveRunCutoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.product = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.product)
        self.runtime = RuntimeStateRoot(
            self.state.resolve(), overlap_paths=(self.product,)
        )
        self.runtime.ensure_layout()
        self.addCleanup(self.runtime.close)
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.plan = self.root / "plan.json"
        self.plan.write_bytes(_plan_bytes())
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref=str(self.plan.resolve())
        )
        self.run_id = "run-cutover"
        sch.create_factory_run(
            store=self.store,
            run_id=self.run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=gitpub.bind_target_worktree(self.product, "refs/heads/main"),
        )
        self.lane = self.store.active_projection(self.run_id)[0]
        self.maestro_file = _install_deployment(self.product, self.state)

    def _plant(self, kind: str) -> None:
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO lane_artifacts (artifact_id, run_id, lane_id, sequence, "
                "completed_stage, artifact_kind, plan_revision, spec_digest, "
                "lane_projection_digest, input_digest, output_digest, artifact_ref, "
                "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "historical-" + kind,
                    self.run_id,
                    self.lane.lane_id,
                    900,
                    st.LaneStage.REVIEWING_TESTS.value,
                    kind,
                    1,
                    self.lane.spec_digest,
                    self.lane.lane_projection_digest,
                    "ab" * 32,
                    "cd" * 32,
                    "vault:historical",
                    "{}",
                    "2026-09-01T00:00:00Z",
                ),
            )

    def _verb(self, argv: list[str]) -> tuple[int, dict]:
        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch.object(maestro, "register_installation"),
            mock.patch.object(maestro, "maybe_autoload_dashboard"),
            mock.patch.object(maestro, "_actor_for", side_effect=_Launched),
        ):
            return _outcome(argv)

    def test_each_historical_kind_refuses_resume_and_status_by_name(self) -> None:
        self.assertEqual(
            set(st.HISTORICAL_LANE_ARTIFACT_KINDS),
            {"SEALED_TEST_BUNDLE", "TEST_INVALIDATION"},
        )
        for kind in st.HISTORICAL_LANE_ARTIFACT_KINDS:
            with self.subTest(kind=kind):
                self.store.conn.execute(
                    "DELETE FROM lane_artifacts WHERE artifact_id LIKE 'historical-%'"
                )
                self.store.conn.commit()
                self._plant(kind)
                for argv in (
                    ["run", "resume", self.run_id],
                    ["run", "status", self.run_id],
                ):
                    code, payload = self._verb(argv)
                    self.assertEqual(code, 3, (argv, payload))
                    self.assertEqual(payload["outcome"], "LIVE_RUN_CUTOVER_REQUIRED")
                    self.assertEqual(
                        payload["detail"],
                        "LIVE_RUN_CUTOVER_REQUIRED:{0}:{1}".format(self.run_id, kind),
                    )

    def test_every_stored_kind_reader_refuses_by_name(self) -> None:
        """The readers below the bind check refuse the same way, never ValueError."""
        self._plant("SEALED_TEST_BUNDLE")
        with self.assertRaises(st.LiveRunCutoverRequired) as caught:
            sch.recorded_artifact_kinds(self.store, self.run_id)
        self.assertEqual(
            str(caught.exception),
            "LIVE_RUN_CUTOVER_REQUIRED:{0}:SEALED_TEST_BUNDLE".format(self.run_id),
        )
        with self.assertRaises(st.LiveRunCutoverRequired):
            self.store.get_lane_artifact("historical-SEALED_TEST_BUNDLE")
        with self.assertRaises(st.LiveRunCutoverRequired):
            sch._lane_artifact_by_id(
                self.store,
                self.run_id,
                self.lane.lane_id,
                "historical-SEALED_TEST_BUNDLE",
            )

    def test_a_ledger_without_historical_kinds_is_unaffected(self) -> None:
        sch.refuse_historical_artifacts(self.store, self.run_id)
        self.assertEqual(sch.recorded_artifact_kinds(self.store, self.run_id), ())
        # Both verbs get past binding to the actor, where the refusing ledger
        # never arrives: nothing here is refused, cutover or otherwise.
        for argv in (["run", "resume", self.run_id], ["run", "status", self.run_id]):
            with self.subTest(argv=argv):
                with self.assertRaises(_Launched):
                    self._verb(argv)


if __name__ == "__main__":
    unittest.main()
