"""One real pytest execution decides admission: failing, instrumentation, passing, too many.

Every case here runs a real pytest process over a real file and classifies the
JUnit report that process wrote. Nothing stubs `subprocess`, so a parser that
reads the report differently from pytest writing it is observable.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from probe_support import pytest_runner, write_tree

from adw_modules import probes
from adw_modules import runner_resolution as rr

PRODUCT = {"refund.py": "def refund(a):\n    return a\n", "pytest.ini": "[pytest]\npythonpath = .\n"}


class AdmissionFromOneRun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = write_tree(Path(self.tmp.name) / "tree", dict(PRODUCT))
        self.scratch = Path(self.tmp.name) / "scratch"

    def _run(self, path: str, source: str):
        write_tree(self.tree, {path: source})
        return probes.run_probe(pytest_runner(), self.tree, path, self.scratch)

    def test_a_failing_probe_is_admitted_for_acceptance_with_its_ids_frozen(self) -> None:
        execution = self._run(
            "tests/test_p.py",
            "from refund import refund\n\n\ndef test_a():\n    assert refund(0) is None\n\n\n"
            "def test_b():\n    assert refund(1) == 1\n",
        )
        self.assertEqual(probes.classify_admission(execution), probes.ADMISSION_FAILING)
        self.assertEqual(
            probes.frozen_case_ids(execution),
            ("tests.test_p::test_a", "tests.test_p::test_b"),
        )
        self.assertEqual(execution.cases["tests.test_p::test_a"], rr.PROBE_FAILED_STATE)
        self.assertEqual(execution.cases["tests.test_p::test_b"], rr.PROBE_PASSED)
        self.assertIn("assert", execution.output)

    def test_a_probe_that_errors_is_the_reviewers_instrumentation_fault(self) -> None:
        execution = self._run(
            "tests/test_p.py",
            "import pytest\n\n\n@pytest.fixture\ndef boom():\n    raise RuntimeError('x')\n\n\n"
            "def test_a(boom):\n    assert True\n",
        )
        self.assertEqual(execution.cases["tests.test_p::test_a"], rr.PROBE_ERROR)
        self.assertEqual(
            probes.classify_admission(execution), probes.PROBE_INSTRUMENTATION_ERROR
        )

    def test_an_import_error_in_the_probe_is_instrumentation_too(self) -> None:
        execution = self._run("tests/test_p.py", "import no_such_module_xyz\n")
        self.assertEqual(
            probes.classify_admission(execution), probes.PROBE_INSTRUMENTATION_ERROR
        )

    def test_a_probe_that_passes_the_first_time_is_not_admitted(self) -> None:
        execution = self._run(
            "tests/test_p.py",
            "from refund import refund\n\n\ndef test_a():\n    assert refund(1) == 1\n",
        )
        self.assertEqual(execution.returncode, 0)
        self.assertEqual(
            probes.classify_admission(execution), probes.ADMISSION_PASSED_ON_FIRST_RUN
        )

    def test_more_than_twenty_cases_is_rejected(self) -> None:
        execution = self._run(
            "tests/test_p.py",
            "import pytest\n\n\n@pytest.mark.parametrize('n', range(21))\n"
            "def test_a(n):\n    assert n < 0\n",
        )
        self.assertEqual(len(execution.cases), 21)
        self.assertEqual(probes.classify_admission(execution), probes.PROBE_TOO_MANY_CASES)

    def test_exactly_twenty_cases_is_admitted(self) -> None:
        execution = self._run(
            "tests/test_p.py",
            "import pytest\n\n\n@pytest.mark.parametrize('n', range(20))\n"
            "def test_a(n):\n    assert n < 0\n",
        )
        self.assertEqual(probes.classify_admission(execution), probes.ADMISSION_FAILING)

    def test_a_nonzero_exit_with_only_passing_cases_is_incomplete_not_failing(self) -> None:
        # A conftest hook turns a green run red at session end: the case lines
        # all say PASSED and the process exits 1. No candidate failure exists.
        write_tree(
            self.tree,
            {
                "conftest.py": "def pytest_sessionfinish(session, exitstatus):\n"
                "    session.exitstatus = 1\n"
            },
        )
        execution = self._run(
            "tests/test_p.py", "def test_a():\n    assert True\n"
        )
        self.assertEqual(set(execution.cases.values()), {rr.PROBE_PASSED})
        self.assertNotEqual(execution.returncode, 0)
        self.assertEqual(probes.classify_admission(execution), probes.PROBE_RUN_INCOMPLETE)

    def test_the_probe_file_bytes_are_unchanged_by_the_measurement(self) -> None:
        source = "def test_a():\n    assert False\n"
        write_tree(self.tree, {"tests/test_p.py": source})
        path = self.tree / "tests" / "test_p.py"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        tree_before = sorted(
            str(p.relative_to(self.tree)) for p in self.tree.rglob("*") if "__pycache__" not in p.parts
        )
        probes.run_probe(pytest_runner(), self.tree, "tests/test_p.py", self.scratch)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        tree_after = sorted(
            str(p.relative_to(self.tree)) for p in self.tree.rglob("*") if "__pycache__" not in p.parts
        )
        self.assertEqual(tree_after, tree_before, "the measurement wrote into the tree it measures")


class DynamicParametrizationEarnsNothing(unittest.TestCase):
    """Case ids that depend on the candidate cannot discharge a probe."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = write_tree(Path(self.tmp.name) / "tree", dict(PRODUCT))
        self.scratch = Path(self.tmp.name) / "scratch"
        self.source = (
            "import pytest\nfrom pathlib import Path\n\n\n"
            "@pytest.mark.parametrize('n', range(int(Path('count').read_text())))\n"
            "def test_a(n):\n    assert Path('verdict').read_text() == 'green'\n"
        )
        write_tree(self.tree, {"tests/test_p.py": self.source})

    def _run(self, count: int, verdict: str):
        write_tree(self.tree, {"count": str(count), "verdict": verdict})
        return probes.run_probe(pytest_runner(), self.tree, "tests/test_p.py", self.scratch)

    def test_growing_the_case_set_reads_open_even_when_every_case_passes(self) -> None:
        admitted = self._run(1, "red")
        self.assertEqual(probes.classify_admission(admitted), probes.ADMISSION_FAILING)
        frozen = probes.frozen_case_ids(admitted)
        green_and_grown = self._run(3, "green")
        self.assertEqual(green_and_grown.returncode, 0)
        self.assertEqual(set(green_and_grown.cases.values()), {rr.PROBE_PASSED})
        self.assertFalse(probes.discharge(green_and_grown, frozen))

    def test_shrinking_the_case_set_reads_open(self) -> None:
        frozen = probes.frozen_case_ids(self._run(2, "red"))
        self.assertFalse(probes.discharge(self._run(1, "green"), frozen))

    def test_the_same_frozen_cases_passing_discharge(self) -> None:
        frozen = probes.frozen_case_ids(self._run(2, "red"))
        self.assertTrue(probes.discharge(self._run(2, "green"), frozen))


class ReviewProgress(unittest.TestCase):
    """Progress is a tuple, never a sum; a probe-less review is the plain integer."""

    @staticmethod
    def _payload(passed=10, records=(), retired=(), status=None):
        payload = {"public_result_summary": {"passed": passed}}
        if records:
            payload["probe_records"] = [{"probe_id": pid} for pid in records]
            payload["probe_retired"] = [{"probe_id": pid} for pid in retired]
            payload["probe_status"] = status or {}
        return payload

    def test_without_probe_records_it_is_the_suite_passed_integer(self) -> None:
        from adw_modules import code_review as cr

        self.assertEqual(cr.review_progress(self._payload(10)), 10)
        self.assertIsNone(cr.review_progress({"public_result_summary": {}}))

    def test_with_records_it_is_suite_discharged_and_active(self) -> None:
        from adw_modules import code_review as cr

        payload = self._payload(
            9, ["p-b", "p-a", "p-c"], ["p-c"], {"p-a": "DISCHARGED", "p-b": "OPEN", "p-c": "DISCHARGED"}
        )
        self.assertEqual(cr.review_progress(payload), (9, ("p-a",), ("p-a", "p-b")))

    def test_a_retired_probe_is_neither_active_nor_discharged(self) -> None:
        from adw_modules import code_review as cr

        payload = self._payload(10, ["p-a"], ["p-a"], {"p-a": "DISCHARGED"})
        self.assertEqual(cr.review_progress(payload), (10, (), ()))


if __name__ == "__main__":
    unittest.main()
