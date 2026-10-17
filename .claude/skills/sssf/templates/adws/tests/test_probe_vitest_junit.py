"""vitest's JUnit report supports the same single-run discharge as pytest's.

Real vitest, installed into a throwaway directory (the pattern of the other
tests that run an installed JS runner): a stubbed `subprocess.run` cannot
observe how vitest reads `--reporter=junit --outputFile=…`. If this ever stops
passing the plan's fallback is a typed `PROBE_RUNNER_UNSUPPORTED` for vitest
lanes, not a weaker reading of the report.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

from probe_support import write_tree

from adw_modules import probes
from adw_modules import runner_resolution as rr

PROBE = "probe.test.js"
PASSING = (
    "import { test, expect, describe } from 'vitest'\n"
    "describe('suite', () => {\n"
    "  test('ok', () => { expect(1).toBe(1) })\n"
    "  test('also ok', () => { expect(2).toBe(2) })\n"
    "})\n"
)
FAILING = PASSING.replace("expect(2).toBe(2)", "expect(2).toBe(3)")
TRUNCATE = textwrap.dedent(
    """
    import subprocess, sys
    args = sys.argv[2:]
    code = subprocess.call([sys.argv[1], *args])
    path = next(a.split("=", 1)[1] for a in args if a.startswith("--outputFile="))
    data = open(path).read()
    open(path, "w").write(data[: len(data) // 2])
    sys.exit(code)
    """
)

_TOOLS: Path | None = None
_TMP: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _TOOLS, _TMP
    npm = shutil.which("npm")
    if npm is None:
        warnings.warn("npm is not installed; the real-vitest probe test cannot run")
        raise unittest.SkipTest("npm is not installed")
    _TMP = tempfile.TemporaryDirectory(prefix="probe-vitest-")
    root = Path(_TMP.name)
    (root / "package.json").write_text('{"name":"x","private":true,"type":"module"}')
    done = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund", "--prefer-offline", "vitest"],
        cwd=root, capture_output=True, text=True, timeout=600,
    )
    if done.returncode != 0:
        _TMP.cleanup()
        raise AssertionError("npm install vitest failed: " + done.stderr[-500:])
    _TOOLS = root


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


class VitestProbeReport(unittest.TestCase):
    def setUp(self) -> None:
        assert _TOOLS is not None
        self.tree = _TOOLS
        self.scratch = Path(tempfile.mkdtemp(prefix="probe-scratch-"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.runner = rr.ResolvedRunner(
            runner="vitest", executable=str(_TOOLS / "node_modules" / ".bin" / "vitest")
        )

    def _run(self, source: str, runner: rr.ResolvedRunner | None = None):
        write_tree(self.tree, {PROBE: source})
        return rr.execute_probe(runner or self.runner, self.tree, PROBE, self.scratch)

    def test_a_passing_probe_reports_every_case_and_discharges(self) -> None:
        execution = self._run(PASSING)
        self.assertEqual(execution.returncode, 0)
        self.assertEqual(execution.report_error, "")
        self.assertEqual(
            execution.cases,
            {
                "probe.test.js::suite > ok": rr.PROBE_PASSED,
                "probe.test.js::suite > also ok": rr.PROBE_PASSED,
            },
        )
        self.assertEqual(probes.classify_admission(execution), probes.ADMISSION_PASSED_ON_FIRST_RUN)
        self.assertTrue(probes.discharge(execution, probes.frozen_case_ids(execution)))

    def test_a_failing_probe_is_admitted_with_the_runners_own_failure_text(self) -> None:
        execution = self._run(FAILING)
        self.assertNotEqual(execution.returncode, 0)
        self.assertEqual(execution.cases["probe.test.js::suite > also ok"], rr.PROBE_FAILED_STATE)
        self.assertEqual(execution.cases["probe.test.js::suite > ok"], rr.PROBE_PASSED)
        self.assertEqual(probes.classify_admission(execution), probes.ADMISSION_FAILING)
        self.assertIn("expected 2 to be 3", execution.output)
        self.assertFalse(probes.discharge(execution, probes.frozen_case_ids(execution)))

    def test_a_partial_report_is_incomplete_never_admitted(self) -> None:
        wrapper = self.scratch / "truncate.py"
        wrapper.write_text(TRUNCATE)
        damaged = rr.ResolvedRunner(
            runner="vitest",
            executable=sys.executable,
            launcher_args=(str(wrapper), self.runner.executable),
        )
        execution = self._run(FAILING, damaged)
        self.assertEqual(execution.report_error, rr.PROBE_REPORT_UNPARSEABLE)
        self.assertEqual(probes.classify_admission(execution), probes.PROBE_RUN_INCOMPLETE)

    def test_a_vitest_setup_error_reads_as_failed_so_the_acceptor_is_the_guard(self) -> None:
        # Pinned, not hidden: vitest's JUnit reports a module-resolution error
        # as a `failure`, so the mechanical ERROR split that catches pytest
        # instrumentation faults does not fire here. The failure text reaches
        # the acceptor, which is where the plan puts that judgment.
        execution = self._run("import { nothing } from './missing.js'\n")
        self.assertEqual(probes.classify_admission(execution), probes.ADMISSION_FAILING)
        self.assertIn("Cannot find module", execution.output)


if __name__ == "__main__":
    unittest.main()
