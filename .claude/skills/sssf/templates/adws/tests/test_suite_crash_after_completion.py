"""A signal after the session finished does not erase the per-case results.

An accepted pytest suite printed `2 failed, 106 passed in 58.70s` and then
SIGSEGV'd in interpreter teardown, every round. `run_suite` read the signal
alone and recorded `SUITE_CRASHED`, so a suite whose every case had an
outcome measured nothing. The rule pinned here: when pytest wrote its
complete JUnit report -- which it does once, in `pytest_sessionfinish` --
those results are the measurement and the crash is a note beside them; a
crash before that point is still `SUITE_CRASHED`.
"""

from __future__ import annotations

import signal
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adw_modules import code_review as cr
from adw_modules import runner_resolution as rr
from adw_modules import scheduler_types as st
from adw_modules import test_binding as tb
from adw_modules import tests_chain as tc

FILES = ("tests/test_a.py",)
SEGV = -int(signal.SIGSEGV)

_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="{total}">{cases}</testsuite></testsuites>
"""


def _junit(passed: int, failed: int) -> str:
    cases = "".join(
        '<testcase classname="tests.test_a" name="test_p{0}"/>'.format(i)
        for i in range(passed)
    ) + "".join(
        '<testcase classname="tests.test_a" name="test_f{0}">'
        '<failure message="assert 1 == 2"/></testcase>'.format(i)
        for i in range(failed)
    )
    return _JUNIT.format(total=passed + failed, cases=cases)


def _report_path(gate: SimpleNamespace) -> Path:
    for arg in gate.argv:
        if arg.startswith("--junitxml="):
            return Path(arg.split("=", 1)[1])
    raise AssertionError("the suite was run with no JUnit report path")


class _Scripted:
    """`execute_cases`, scripted: what the runner printed, wrote and exited with."""

    def __init__(self, *, output: str, returncode: int, junit: str | None) -> None:
        self.output = output
        self.returncode = returncode
        self.junit = junit

    def __call__(self, resolved, gate, tree, **_kw):
        if self.junit is not None:
            _report_path(gate).write_text(self.junit, encoding="utf-8")
        return {"output": self.output, "returncode": self.returncode}


def _run(script: _Scripted) -> dict:
    with tempfile.TemporaryDirectory() as tree:
        with mock.patch.object(
            rr,
            "resolve",
            return_value=rr.ResolvedRunner(runner="pytest", executable="/bin/x"),
        ), mock.patch.object(rr, "execute_cases", side_effect=script):
            return tc.run_suite(Path(tree), FILES)


class CrashAfterCompletion(unittest.TestCase):
    def test_a_crash_after_a_complete_report_counts_the_results(self) -> None:
        run = _run(
            _Scripted(
                output="test_a.py::test_p0 PASSED\n== 2 failed, 3 passed in 1.20s ==\n"
                "Fatal Python error: Segmentation fault\n",
                returncode=SEGV,
                junit=_junit(passed=3, failed=2),
            )
        )
        self.assertNotIn("crashed", run)
        self.assertEqual(
            run["counts"], {"passed": 3, "failed": 2, "errored": 0, "skipped": 0}
        )
        self.assertEqual(run["executed"], 5)
        self.assertEqual(run["crash_note"], "SUITE_CRASHED_AFTER_COMPLETION:SIGSEGV")
        self.assertIn("Fatal Python error: Segmentation fault", run["crash_tail"])
        self.assertEqual(run["crash_returncode"], SEGV)
        # The exit status the results imply, not the one the signal left.
        self.assertEqual(run["returncode"], 1)

    def test_an_all_green_suite_that_crashes_after_completion_is_green(self) -> None:
        run = _run(
            _Scripted(
                output="== 4 passed in 0.50s ==\n",
                returncode=128 + int(signal.SIGSEGV),
                junit=_junit(passed=4, failed=0),
            )
        )
        self.assertEqual(run["returncode"], 0)
        self.assertEqual(run["counts"]["passed"], 4)
        self.assertEqual(run["crash_note"], "SUITE_CRASHED_AFTER_COMPLETION:SIGSEGV")

    def test_a_crash_before_the_report_is_written_still_blocks(self) -> None:
        run = _run(
            _Scripted(
                output="test_a.py::test_p0 PASSED\nFatal Python error: Bus error\n",
                returncode=-int(signal.SIGBUS),
                junit=None,
            )
        )
        self.assertEqual(run["crashed"], "SUITE_CRASHED:SIGBUS")
        self.assertNotIn("crash_note", run)

    def test_a_complete_report_stands_when_the_printed_totals_were_lost(self) -> None:
        # The totals line sat in a pipe buffer the signal never flushed.
        run = _run(
            _Scripted(
                output="test_a.py::test_p0 PASSED\n",
                returncode=SEGV,
                junit=_junit(passed=1, failed=0),
            )
        )
        self.assertNotIn("crashed", run)
        self.assertEqual(run["counts"]["passed"], 1)
        self.assertEqual(run["returncode"], 0)

    def test_a_truncated_report_still_blocks(self) -> None:
        run = _run(
            _Scripted(
                output="== 3 passed in 0.10s ==\n",
                returncode=SEGV,
                junit=_junit(passed=3, failed=0)[:60],
            )
        )
        self.assertEqual(run["crashed"], "SUITE_CRASHED:SIGSEGV")


class TheCrashNoteReachesTheReview(unittest.TestCase):
    """`measure_candidate` hands builder and reviewer the counts and the note."""

    def _measure(self, run: dict) -> cr.SuiteMeasurement:
        with tempfile.TemporaryDirectory() as scratch, mock.patch.object(
            tc, "suite_files", return_value={"tests/test_a.py": "blob"}
        ), mock.patch.object(cr, "_review_tree"), mock.patch.object(
            tb, "verify_suite"
        ), mock.patch.object(cr, "_run_suite", return_value=(run, None)):
            return cr.measure_candidate(
                request=SimpleNamespace(lane_id="lane-a", input_digest="0" * 64),
                state_root=Path(scratch),
                candidate_repo=Path(scratch),
                candidate_sha="a" * 40,
                candidate_ref="refs/maestro/candidates/x",
                builder_base_sha="b" * 40,
                accepted_suite=SimpleNamespace(
                    kind=st.ArtifactKind.ACCEPTED_TEST_SUITE, payload={}
                ),
                scratch_root=Path(scratch),
            )

    def test_a_green_suite_with_a_late_crash_is_not_a_failure(self) -> None:
        measured = self._measure(
            {
                "counts": {"passed": 4, "failed": 0, "errored": 0, "skipped": 0},
                "executed": 4,
                "min_cases": 1,
                "output": "== 4 passed in 0.50s ==",
                "returncode": 0,
                "crash_note": "SUITE_CRASHED_AFTER_COMPLETION:SIGSEGV",
                "crash_tail": ["Fatal Python error: Segmentation fault"],
            }
        )
        self.assertFalse(measured.runner_failed)
        self.assertNotIn("crashed", measured.summary)
        self.assertEqual(
            measured.summary["crash_note"], "SUITE_CRASHED_AFTER_COMPLETION:SIGSEGV"
        )
        self.assertEqual(
            measured.summary["crash_tail"], ["Fatal Python error: Segmentation fault"]
        )


class MeasuredAgainstRealPytest(unittest.TestCase):
    """The report path and the completeness rule, run under the real binary."""

    def _tree(self, root: Path, conftest: str) -> None:
        (root / "tests").mkdir()
        (root / "tests" / "test_a.py").write_text(
            "def test_one():\n    assert True\n\n"
            "def test_two():\n    assert 1 == 2\n",
            encoding="utf-8",
        )
        (root / "conftest.py").write_text(textwrap.dedent(conftest), encoding="utf-8")

    def _run(self, conftest: str) -> dict:
        with tempfile.TemporaryDirectory() as holder:
            root = Path(holder)
            self._tree(root, conftest)
            real = rr.ResolvedRunner(
                runner="pytest",
                executable=sys.executable,
                launcher_args=("-m", "pytest"),
                origin="declared",
            )
            with mock.patch.object(rr, "resolve", return_value=real):
                return tc.run_suite(root, FILES)

    def test_a_segfault_after_session_finish_keeps_the_results(self) -> None:
        run = self._run(
            """
            import os, signal

            def pytest_unconfigure(config):
                os.kill(os.getpid(), signal.SIGSEGV)
            """
        )
        self.assertNotIn("crashed", run)
        self.assertEqual(run["counts"]["passed"], 1)
        self.assertEqual(run["counts"]["failed"], 1)
        self.assertEqual(run["crash_note"], "SUITE_CRASHED_AFTER_COMPLETION:SIGSEGV")
        self.assertEqual(run["returncode"], 1)

    def test_a_segfault_inside_a_test_is_still_a_crash(self) -> None:
        run = self._run(
            """
            import os, signal

            def pytest_runtest_call(item):
                if item.name == "test_two":
                    os.kill(os.getpid(), signal.SIGSEGV)
            """
        )
        self.assertEqual(run["crashed"], "SUITE_CRASHED:SIGSEGV")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
