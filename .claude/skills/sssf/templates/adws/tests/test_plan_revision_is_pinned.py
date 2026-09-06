"""A recorded plan revision is the run's own bytes, not a file you can edit.

Measured 2026-09-06 on FDAdb run `2489c772d7c04ad5a2f2bcaa2f4de11c`. Its
`plan_revisions.plan_artifact_ref` was
`/Users/davidandrews/PycharmProjects/FDAdb/.maestro/plans/fdadb-wp4/maestro-plan.v1`
-- a path into the operator's working tree -- and `runtime_state_root/plans/`
was empty.

`_bind_existing_run` recompiles that path on every bind and refuses
`PLAN_ARTIFACT_MISMATCH` if the digest moved. It gates `resume`, `amend` and
`status` alike, so editing the plan an amendment requires stopped all three
verbs at once, and the only way back was to restore the exact bytes. Which
means the plan could not be amended in place at all: the operator had to know,
with nothing saying so, that every revision needs its own directory.

`MAESTRO_architecture.md` already places copied plans under the deployment's
`runtime_state_root`. The empty directory was the receipt that the copy was
never written.

What did *not* change is the part that could have moved every digest in the
ledger: `plan_artifact_ref` keeps the value it was recorded with, and
`_compile_plan` is still handed that string as `ref`. Only the bytes it reads
come from somewhere the operator cannot reach.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

import maestro


class ThePinnedPathIsPerRunPerRevision(unittest.TestCase):
    def test_it_sits_under_the_runtime_state_root(self) -> None:
        runtime = SimpleNamespace(path=Path("/state/fdadb"))

        pinned = maestro._pinned_plan_artifact(runtime, "abc123", 2)

        self.assertEqual(
            pinned, Path("/state/fdadb/plans/abc123/r2/maestro-plan.v1")
        )

    def test_two_revisions_of_one_run_do_not_share_a_file(self) -> None:
        runtime = SimpleNamespace(path=Path("/state/fdadb"))

        first = maestro._pinned_plan_artifact(runtime, "abc123", 1)
        second = maestro._pinned_plan_artifact(runtime, "abc123", 2)

        # A recorded revision is immutable; an amendment appends one rather
        # than rewriting the one every existing lane artifact is bound to.
        self.assertNotEqual(first, second)

    def test_two_runs_of_one_plan_do_not_share_a_file(self) -> None:
        runtime = SimpleNamespace(path=Path("/state/fdadb"))

        self.assertNotEqual(
            maestro._pinned_plan_artifact(runtime, "aaa", 1),
            maestro._pinned_plan_artifact(runtime, "bbb", 1),
        )


class PinningCopiesOnceAndNeverOverwrites(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "maestro-plan.v1"
        self.source.write_text("revision one", encoding="utf-8")
        self.pinned = self.root / "state" / "plans" / "run" / "r1" / "maestro-plan.v1"

    def test_it_creates_the_copy_and_its_parents(self) -> None:
        maestro._pin_plan_artifact(self.source, self.pinned)

        self.assertTrue(self.pinned.is_file())
        self.assertEqual(self.pinned.read_text(encoding="utf-8"), "revision one")

    def test_an_edit_to_the_source_does_not_reach_the_pin(self) -> None:
        maestro._pin_plan_artifact(self.source, self.pinned)

        self.source.write_text("edited by the operator", encoding="utf-8")

        self.assertEqual(self.pinned.read_text(encoding="utf-8"), "revision one")

    def test_pinning_again_leaves_the_recorded_bytes_alone(self) -> None:
        # The whole point: a second bind must not re-pin from a source that has
        # since been edited, or the pin is no safer than the path it replaced.
        maestro._pin_plan_artifact(self.source, self.pinned)
        self.source.write_text("edited by the operator", encoding="utf-8")

        maestro._pin_plan_artifact(self.source, self.pinned)

        self.assertEqual(self.pinned.read_text(encoding="utf-8"), "revision one")

    def test_it_leaves_no_scratch_file_behind(self) -> None:
        maestro._pin_plan_artifact(self.source, self.pinned)

        self.assertEqual(
            sorted(item.name for item in self.pinned.parent.iterdir()),
            ["maestro-plan.v1"],
        )


class BindPrefersThePinAndKeepsTheRecordedRef(unittest.TestCase):
    """Source-level, because binding needs a ledger, a runtime and a target."""

    def _bind_body(self) -> str:
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        body = source.split("def _bind_existing_run(", 1)[1]
        return body.split("\ndef _run_resume(", 1)[0]

    def test_the_pin_is_read_and_the_recorded_ref_is_still_the_identity(
        self,
    ) -> None:
        body = self._bind_body()
        self.assertIn("pinned if pinned.is_file() else plan_ref", body)
        # `ref` is what becomes `compiled.plan_artifact_ref` and what
        # `planned_input_digest` hashes. Handing it the pin would move every
        # digest in every existing ledger.
        self.assertIn("ref=str(plan_ref)", body)

    def test_a_run_is_pinned_only_after_its_digest_is_proven(self) -> None:
        body = self._bind_body()
        check = body.index('raise FactoryRefused("PLAN_ARTIFACT_MISMATCH")')
        pin = body.index("_pin_plan_artifact(plan_ref, pinned)")
        self.assertLess(check, pin)

    def test_start_and_amend_pin_the_revision_they_record(self) -> None:
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        start = source.split("def _run_start(", 1)[1].split("\ndef _bind_existing_run(", 1)[0]
        amend = source.split("def _run_amend(", 1)[1].split("\ndef _run_status(", 1)[0]
        self.assertIn("_pin_plan_artifact(", start)
        self.assertIn("_pin_plan_artifact(", amend)
        # An amendment that records revision N+1 and pins nothing leaves the
        # next bind reading the operator's file again, which is the trap. The
        # revision is the one the caller is recording, not one read back off
        # the compiled object.
        self.assertIn('row["plan_revision"] + 1', amend)
        self.assertIn("_pinned_plan_artifact(runtime, run_id, 1)", start)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
