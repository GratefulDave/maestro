"""A finished suite measurement is not thrown away when its session ends.

Run 2acd2f5f: an interrupt paused a lane whose worker was inside the accepted
suite. The worker finished (12 minutes), the ledger refused its late
completion, and the next session ran the same candidate's suite again.
`measure_candidate` now keeps the finished `run` under the state root, keyed on
every input it depends on, and a measurement of those same inputs reads it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adw_modules import code_review as cr
from adw_modules import scheduler_types as st
from adw_modules import test_binding as tb
from adw_modules import tests_chain as tc

SHA = "a" * 40
BASE = "b" * 40
GATE = SimpleNamespace(argv=("pytest",), min_cases=1)


def _run(passed: int = 3, failed: int = 0, **extra: object) -> dict:
    return {
        "counts": {"passed": passed, "failed": failed, "errored": 0, "skipped": 0},
        "executed": passed + failed,
        "min_cases": 1,
        "output": "ok",
        "returncode": 1 if failed else 0,
        "runner": "pytest",
        **extra,
    }


class SuiteMeasurementSurvivesInterrupt(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def _measure(self, runs: list, *, digest: str = "d1", sha: str = SHA, gate=GATE):
        calls = []

        def run_suite(tree, files, *, gate, suite_timeout_s=None, **_provisioning):
            calls.append(1)
            return runs.pop(0)

        with mock.patch.object(cr, "_review_tree", return_value=self.root), mock.patch.object(
            tb, "verify_suite"
        ), mock.patch.object(
            tc, "suite_files", return_value={"tests/test_a.py": "h"}
        ), mock.patch.object(cr, "_run_suite", side_effect=run_suite):
            measurement = cr.measure_candidate(
                request=SimpleNamespace(lane_id="lane-a", input_digest=digest),
                state_root=self.root / "state",
                candidate_repo=self.root,
                candidate_sha=sha,
                candidate_ref="refs/maestro/candidates/r/lane-a/x",
                builder_base_sha=BASE,
                accepted_suite=SimpleNamespace(kind=st.ArtifactKind.ACCEPTED_TEST_SUITE),
                scratch_root=self.root / "scratch",
                gate=gate,
            )
        return measurement, len(calls)

    def test_the_same_inputs_are_not_measured_twice(self) -> None:
        first, ran = self._measure([(_run(), None)])
        self.assertEqual(ran, 1)
        second, ran = self._measure([])
        self.assertEqual(ran, 0)
        self.assertEqual(second.summary, first.summary)
        self.assertFalse(second.runner_failed)

    def test_a_failing_measurement_is_kept_too(self) -> None:
        self._measure([(_run(passed=2, failed=1), None)])
        again, ran = self._measure([])
        self.assertEqual(ran, 0)
        self.assertTrue(again.runner_failed)

    def test_another_candidate_or_stage_input_is_measured_afresh(self) -> None:
        self._measure([(_run(), None)])
        for kwargs in ({"sha": "c" * 40}, {"digest": "d2"}, {"gate": SimpleNamespace(argv=("x",))}):
            _measurement, ran = self._measure([(_run(), None)], **kwargs)
            self.assertEqual(ran, 1, kwargs)

    def test_a_crashed_run_is_not_kept(self) -> None:
        self._measure([(_run(passed=0, crashed="SUITE_CRASHED:SIGSEGV"), None)])
        _measurement, ran = self._measure([(_run(), None)])
        self.assertEqual(ran, 1)


if __name__ == "__main__":
    unittest.main()
