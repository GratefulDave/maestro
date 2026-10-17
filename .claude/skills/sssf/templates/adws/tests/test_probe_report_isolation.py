"""Two probe executions never share a report path, and never read an old one.

Real pytest throughout. The interleaving is forced, not hoped for: a wrapper
around `rr.execute_cases` makes both executions finish their processes -- both
reports written -- before either reads its own, which is the schedule on which
a shared `report.xml` returns one run's states to the other.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from probe_support import pytest_runner, write_tree

from adw_modules import runner_resolution as rr

PROBE = "tests/test_p.py"
PINI = {"pytest.ini": "[pytest]\npythonpath = .\n"}


def _tree(root: Path, name: str, body: str) -> Path:
    return write_tree(root / name, {**PINI, PROBE: body})


class IdenticalIdsShareNoPath(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scratch = self.root / "scratch"

    def test_concurrent_executions_with_identical_ids_keep_their_own_states(self) -> None:
        green = _tree(self.root, "green", "def test_a():\n    assert True\n")
        red = _tree(self.root, "red", "def test_a():\n    assert False\n")
        barrier = threading.Barrier(2, timeout=60)
        real = rr.execute_cases

        def after_both_wrote(*args, **kwargs):
            result = real(*args, **kwargs)
            barrier.wait()
            return result

        results: dict[str, rr.ProbeExecution] = {}

        def run(name: str, tree: Path) -> None:
            results[name] = rr.execute_probe(pytest_runner(), tree, PROBE, self.scratch)

        with mock.patch.object(rr, "execute_cases", side_effect=after_both_wrote):
            threads = [
                threading.Thread(target=run, args=("green", green)),
                threading.Thread(target=run, args=("red", red)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(120)
        self.assertEqual(results["green"].cases, {"tests.test_p::test_a": rr.PROBE_PASSED})
        self.assertEqual(results["green"].returncode, 0)
        self.assertEqual(results["red"].cases, {"tests.test_p::test_a": rr.PROBE_FAILED_STATE})
        self.assertNotEqual(results["red"].returncode, 0)
        self.assertIn("assert", results["red"].output)

    def test_a_run_that_writes_no_report_never_reuses_an_earlier_one(self) -> None:
        tree = _tree(self.root, "t", "def test_a():\n    assert True\n")
        first = rr.execute_probe(pytest_runner(), tree, PROBE, self.scratch)
        self.assertEqual(first.report_error, "")
        silent = rr.ResolvedRunner(
            runner="pytest", executable=sys.executable, launcher_args=("-c", "import sys; sys.exit(0)")
        )
        second = rr.execute_probe(silent, tree, PROBE, self.scratch)
        self.assertEqual(second.report_error, rr.PROBE_REPORT_MISSING)
        self.assertEqual(second.cases, {})

    def test_the_directory_is_removed_on_success_failure_and_timeout(self) -> None:
        reports = self.scratch / "probe-reports"
        ok = _tree(self.root, "ok", "def test_a():\n    assert True\n")
        bad = _tree(self.root, "bad", "def test_a():\n    assert False\n")
        rr.execute_probe(pytest_runner(), ok, PROBE, self.scratch)
        rr.execute_probe(pytest_runner(), bad, PROBE, self.scratch)
        hang = rr.ResolvedRunner(
            runner="pytest",
            executable=sys.executable,
            launcher_args=("-c", "import time; time.sleep(60)"),
        )
        timed_out = rr.execute_probe(hang, ok, PROBE, self.scratch, timeout_s=2.0)
        self.assertEqual(timed_out.report_error, rr.PROBE_REPORT_MISSING)
        self.assertEqual(list(reports.iterdir()), [])

    def test_only_the_callers_own_directory_is_removed(self) -> None:
        reports = self.scratch / "probe-reports"
        neighbour = reports / "probe-neighbour"
        neighbour.mkdir(parents=True)
        (neighbour / "report.xml").write_text("<testsuites/>")
        tree = _tree(self.root, "t", "def test_a():\n    assert True\n")
        rr.execute_probe(pytest_runner(), tree, PROBE, self.scratch)
        self.assertEqual([p.name for p in reports.iterdir()], ["probe-neighbour"])
        self.assertEqual((neighbour / "report.xml").read_text(), "<testsuites/>")

    def test_each_call_allocates_a_fresh_directory(self) -> None:
        tree = _tree(self.root, "t", "def test_a():\n    assert True\n")
        seen: list[str] = []
        real = rr.execute_cases

        def record(resolved, gate, tree_, **kwargs):
            seen.extend(arg for arg in gate.argv if arg.startswith("--junitxml="))
            return real(resolved, gate, tree_, **kwargs)

        with mock.patch.object(rr, "execute_cases", side_effect=record):
            rr.execute_probe(pytest_runner(), tree, PROBE, self.scratch)
            rr.execute_probe(pytest_runner(), tree, PROBE, self.scratch)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])


if __name__ == "__main__":
    unittest.main()
