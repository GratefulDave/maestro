"""OBLIGATION_UNDECIDED is judged at ship/start/amend, never against a bound run.

`_bind_existing_run` re-compiles the revision a run holds on every `run resume`,
`run status`, `run attend`, and on the previous revision under `run amend`. A
run bound before a gating obligation had to state `decided_by` examples must
keep resuming; only a plan entering a run is refused.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import plan_validate as pv
from adw_modules import scheduler as sch
from adw_modules.lifecycle import ArtifactStore
from adw_modules.plan_model import PlanCompileError
from adw_modules.runtime_state import RuntimeStateRoot
from tests.test_run_status import _init_repo, _install_deployment, _outcome


def _undecided_plan_bytes() -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-t",
                "lane_kind": "tests",
                "needs": [],
                "outputs": ["tests/test_a.py"],
                "spec": {
                    "goal": "cover a.txt",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": [
                    {
                        "criterion": "claim-a (positive): a.txt holds provenance",
                        "gating": True,
                        "observation_seam": "a.txt is the recorded effect",
                    }
                ],
            }
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class BoundPlanIsNotRejudgedTest(unittest.TestCase):
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
        self.plan.write_bytes(_undecided_plan_bytes())
        # The run was bound before the obligation existed.
        compiled = plan_compiler.compile_plan(
            _undecided_plan_bytes(),
            plan_revision=1,
            plan_artifact_ref=str(self.plan.resolve()),
            bound_run=True,
        )
        self.run_id = "run-bound-before-decided-by"
        sch.create_factory_run(
            store=self.store,
            run_id=self.run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=gitpub.bind_target_worktree(self.product, "refs/heads/main"),
        )
        self.maestro_file = _install_deployment(self.product, self.state)

    def test_the_plan_would_be_refused_entering_a_run(self) -> None:
        with self.assertRaises(PlanCompileError) as caught:
            maestro._compile_plan(self.plan, revision=2, ref=str(self.plan))
        self.assertIn(
            pv.OBLIGATION_UNDECIDED, {item.code for item in caught.exception.refusals}
        )

    def test_binding_the_existing_run_does_not_rejudge_it(self) -> None:
        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch.object(maestro, "register_installation"),
        ):
            _layout, runtime, store, row, _target, compiled = (
                maestro._bind_existing_run(self.run_id)
            )
        try:
            self.assertEqual(compiled.plan_digest, row["plan_digest"])
        finally:
            store.close()
            runtime.close()

    def _status_payload(self, *, strict_bind: bool) -> dict:
        real = maestro._compile_plan

        def compile_plan(path, *, revision, ref, bound_run=False):
            return real(
                path, revision=revision, ref=ref, bound_run=bound_run and not strict_bind
            )

        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch.object(maestro, "register_installation"),
            mock.patch.object(maestro, "_compile_plan", compile_plan),
        ):
            _code, payload = _outcome(["run", "status", self.run_id])
        return payload

    def test_run_status_on_the_bound_run_gets_past_plan_binding(self) -> None:
        """Status proceeds to its own later gates, never OBLIGATION_UNDECIDED.

        The control re-binds strictly and must be refused by the check, which
        is what proves this case observes the binding rather than passing
        because status fails somewhere earlier.
        """
        control = self._status_payload(strict_bind=True)
        self.assertIn(pv.OBLIGATION_UNDECIDED, json.dumps(control))
        payload = self._status_payload(strict_bind=False)
        self.assertNotIn(pv.OBLIGATION_UNDECIDED, json.dumps(payload))
        self.assertNotIn("PLAN", str(payload.get("outcome", "")))


if __name__ == "__main__":
    unittest.main()
