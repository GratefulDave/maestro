"""Discharge reads one execution: exit code, a whole report, the frozen ids, every case PASSED.

Real pytest, real JUnit XML. The report is damaged *after* pytest wrote it by
a conftest hook, so a reader that tolerates a partial file is observable.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from probe_support import pytest_runner, write_tree

from adw_modules import probes
from adw_modules import runner_resolution as rr

PROBE = "tests/test_p.py"
TWO_CASES = "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
DAMAGE = """\
import os


def pytest_unconfigure(config):
    mode = open("mode").read().strip()
    path = config.option.xmlpath
    if mode == "truncate":
        data = open(path).read()
        open(path, "w").write(data[: len(data) // 2])
    elif mode == "delete":
        os.remove(path)
    elif mode == "empty":
        open(path, "w").write("<testsuites></testsuites>")
    elif mode == "garbage":
        open(path, "w").write("not xml at all")
"""


class DischargeBinding(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = write_tree(
            Path(self.tmp.name) / "tree", {"pytest.ini": "[pytest]\npythonpath = .\n"}
        )
        self.scratch = Path(self.tmp.name) / "scratch"

    def _run(self, source: str = TWO_CASES, **extra: str):
        write_tree(self.tree, {PROBE: source, **extra})
        return probes.run_probe(pytest_runner(), self.tree, PROBE, self.scratch)

    def _frozen(self, source: str = TWO_CASES):
        return probes.frozen_case_ids(self._run(source))

    def test_all_passing_cases_and_a_clean_exit_discharge(self) -> None:
        frozen = self._frozen()
        execution = self._run()
        self.assertEqual(execution.returncode, 0)
        self.assertTrue(probes.discharge(execution, frozen))

    def test_every_case_line_passing_but_a_nonzero_exit_is_open(self) -> None:
        frozen = self._frozen()
        hook = "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n"
        execution = self._run(TWO_CASES, **{"conftest.py": hook})
        self.assertEqual(set(execution.cases.values()), {rr.PROBE_PASSED})
        self.assertEqual(set(execution.cases), set(frozen))
        self.assertNotEqual(execution.returncode, 0)
        self.assertFalse(probes.discharge(execution, frozen))
        self.assertEqual(probes.classify_admission(execution), probes.PROBE_RUN_INCOMPLETE)

    def test_a_truncated_report_is_open_and_not_admitted(self) -> None:
        frozen = self._frozen()
        execution = self._run(**{"conftest.py": DAMAGE, "mode": "truncate"})
        self.assertEqual(execution.report_error, rr.PROBE_REPORT_UNPARSEABLE)
        self.assertEqual(execution.cases, {})
        self.assertFalse(probes.discharge(execution, frozen))
        self.assertEqual(probes.classify_admission(execution), probes.PROBE_RUN_INCOMPLETE)

    def test_garbage_in_the_report_is_unparseable(self) -> None:
        execution = self._run(**{"conftest.py": DAMAGE, "mode": "garbage"})
        self.assertEqual(execution.report_error, rr.PROBE_REPORT_UNPARSEABLE)

    def test_a_missing_report_is_open_and_never_a_pass(self) -> None:
        frozen = self._frozen()
        execution = self._run(**{"conftest.py": DAMAGE, "mode": "delete"})
        self.assertEqual(execution.report_error, rr.PROBE_REPORT_MISSING)
        self.assertEqual(execution.returncode, 0)
        self.assertFalse(probes.discharge(execution, frozen))
        self.assertEqual(probes.classify_admission(execution), probes.PROBE_RUN_INCOMPLETE)

    def test_a_report_with_no_testcase_is_empty_and_open(self) -> None:
        frozen = self._frozen()
        execution = self._run(**{"conftest.py": DAMAGE, "mode": "empty"})
        self.assertEqual(execution.report_error, rr.PROBE_REPORT_EMPTY)
        self.assertFalse(probes.discharge(execution, frozen))

    def test_a_pass_plus_a_teardown_error_for_one_id_reduces_to_error(self) -> None:
        source = (
            "import pytest\n\n\n@pytest.fixture\ndef f():\n    yield\n    raise RuntimeError('teardown')\n\n\n"
            "def test_a(f):\n    assert True\n"
        )
        execution = self._run(source)
        self.assertEqual(execution.cases, {"tests.test_p::test_a": rr.PROBE_ERROR})
        self.assertFalse(probes.discharge(execution, ("tests.test_p::test_a",)))
        self.assertEqual(
            probes.classify_admission(execution), probes.PROBE_INSTRUMENTATION_ERROR
        )

    def test_a_call_failure_and_a_clean_teardown_reduce_to_failed(self) -> None:
        execution = self._run("def test_a():\n    assert False\n")
        self.assertEqual(execution.cases, {"tests.test_p::test_a": rr.PROBE_FAILED_STATE})

    def test_equal_cardinality_with_a_different_id_set_is_open(self) -> None:
        frozen = self._frozen(TWO_CASES)
        renamed = self._run(TWO_CASES.replace("test_a", "test_c").replace("test_b", "test_d"))
        self.assertEqual(len(renamed.cases), len(frozen))
        self.assertEqual(renamed.returncode, 0)
        self.assertFalse(probes.discharge(renamed, frozen))

    def test_an_extra_id_and_a_missing_id_are_each_refused(self) -> None:
        frozen = self._frozen(TWO_CASES)
        extra = self._run(TWO_CASES + "\n\ndef test_c():\n    assert True\n")
        self.assertFalse(probes.discharge(extra, frozen))
        missing = self._run("def test_a():\n    assert True\n")
        self.assertFalse(probes.discharge(missing, frozen))

    def test_a_skip_never_discharges(self) -> None:
        source = (
            "import pytest\n\n\ndef test_a():\n    assert True\n\n\n"
            "def test_b():\n    pytest.skip('later')\n"
        )
        frozen = self._frozen(source)
        execution = self._run(source)
        self.assertEqual(execution.returncode, 0)
        self.assertEqual(execution.cases["tests.test_p::test_b"], rr.PROBE_SKIPPED)
        self.assertFalse(probes.discharge(execution, frozen))

    def test_counts_are_never_consulted(self) -> None:
        ids = ("a::x", "a::y")
        lying_green = rr.ProbeExecution(
            0, "1 failed, 0 passed", {"a::x": rr.PROBE_PASSED, "a::y": rr.PROBE_PASSED}
        )
        lying_red = rr.ProbeExecution(
            0, "2 passed", {"a::x": rr.PROBE_PASSED, "a::y": rr.PROBE_FAILED_STATE}
        )
        self.assertTrue(probes.discharge(lying_green, ids))
        self.assertFalse(probes.discharge(lying_red, ids))

    def test_an_empty_frozen_set_never_discharges(self) -> None:
        self.assertFalse(probes.discharge(rr.ProbeExecution(0, "", {"a::x": rr.PROBE_PASSED}), ()))

    def test_the_report_is_written_outside_the_tree(self) -> None:
        self._run()
        self.assertEqual([p for p in self.tree.rglob("*.xml")], [])

    def test_a_report_directory_inside_the_tree_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            rr.execute_probe(pytest_runner(), self.tree, PROBE, self.tree / "scratch")


if __name__ == "__main__":
    unittest.main()
