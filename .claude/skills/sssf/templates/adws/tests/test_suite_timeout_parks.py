"""A suite that outruns its budget is an environment fault, never zero cases.

The accepted suite took longer than the runner's hard-coded 120s, the runner
killed it, a killed run prints no summary line, and the counts parsed as 0.
That read as a candidate that collects nothing, billed the builder its revise
rounds, and parked the lane NO_PROGRESS. The budget is now `suite_timeout_s`
(default 3600s) and a timeout parks as SUITE_TIMEOUT.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import maestro
from adw_modules import code_review as cr
from adw_modules import launcher as lch
from adw_modules import review_contract as rc
from adw_modules import runner_resolution as rr
from adw_modules import scheduler as sch
from adw_modules import tests_chain as tc

_TIMED_OUT = {
    "output": "vitest did not finish executing in 1s",
    "returncode": -1,
    "timed_out": True,
}
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def _vitest() -> rr.ResolvedRunner:
    return rr.ResolvedRunner(runner="vitest", executable="/bin/x")


def _sleeping_runner() -> SimpleNamespace:
    """A resolved runner whose execution is a real process that never finishes."""
    return SimpleNamespace(runner="pytest", execute_argv=lambda argv: list(_SLEEPER))


class ExecuteCasesReportsTheTimeout(unittest.TestCase):
    def test_a_real_process_killed_at_the_budget_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tree:
            raw = rr.execute_cases(
                _sleeping_runner(),
                SimpleNamespace(cwd=".", argv=()),
                Path(tree),
                timeout_s=1,
            )
        self.assertIs(raw["timed_out"], True)
        self.assertEqual(raw["returncode"], -1)

    def test_a_finished_run_says_it_finished(self) -> None:
        resolved = SimpleNamespace(
            runner="pytest", execute_argv=lambda argv: [sys.executable, "-c", "pass"]
        )
        with tempfile.TemporaryDirectory() as tree:
            raw = rr.execute_cases(
                resolved, SimpleNamespace(cwd=".", argv=()), Path(tree), timeout_s=30
            )
        self.assertIs(raw["timed_out"], False)
        self.assertEqual(raw["returncode"], 0)


class RunSuiteParksOnTimeout(unittest.TestCase):
    def test_the_default_budget_is_an_hour(self) -> None:
        self.assertEqual(tc.SUITE_TIMEOUT_S, 3600.0)
        with tempfile.TemporaryDirectory() as tree, mock.patch.object(
            rr, "resolve", return_value=_vitest()
        ), mock.patch.object(
            rr,
            "execute_cases",
            return_value={"output": "Tests  1 passed (1)", "returncode": 0},
        ) as execute:
            tc.run_suite(Path(tree), ("src/a.test.ts",))
        self.assertEqual(execute.call_args.kwargs["timeout_s"], 3600.0)

    def test_a_configured_budget_reaches_the_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tree, mock.patch.object(
            rr, "resolve", return_value=_vitest()
        ), mock.patch.object(
            rr,
            "execute_cases",
            return_value={"output": "Tests  1 passed (1)", "returncode": 0},
        ) as execute:
            tc.run_suite(Path(tree), ("src/a.test.ts",), timeout_s=7200.0)
        self.assertEqual(execute.call_args.kwargs["timeout_s"], 7200.0)

    def test_a_timeout_raises_suite_timeout_not_a_zero_count(self) -> None:
        with tempfile.TemporaryDirectory() as tree, mock.patch.object(
            rr, "resolve", return_value=_vitest()
        ), mock.patch.object(
            rr, "execute_cases", return_value=_TIMED_OUT
        ), self.assertRaises(rc.SuiteTimeoutError) as caught:
            tc.run_suite(Path(tree), ("src/a.test.ts",))
        self.assertTrue(str(caught.exception).startswith("SUITE_TIMEOUT:"))
        self.assertIsInstance(caught.exception, rc.SuiteEnvironmentError)

    def test_a_real_process_past_its_budget_raises_suite_timeout(self) -> None:
        # Only resolution is stubbed: the execution is a real child process that
        # outlives a one-second budget, so `run_bounded` really kills it.
        with tempfile.TemporaryDirectory() as tree, mock.patch.object(
            rr, "resolve", return_value=_sleeping_runner()
        ), mock.patch.object(
            tc, "_assert_declared_python"
        ), self.assertRaises(rc.SuiteTimeoutError) as caught:
            tc.run_suite(Path(tree), ("tests/test_a.py",), timeout_s=1.0)
        self.assertIn("did not finish within 1s", str(caught.exception))


class CodeReviewDoesNotAbsorbATimeout(unittest.TestCase):
    def test_run_suite_arguments_reach_the_runner_through_code_review(self) -> None:
        with mock.patch.object(tc, "run_suite", return_value={}) as run:
            cr._run_suite(Path("t"), {"a.test.ts": "x"}, gate=None, suite_timeout_s=99.0)
        self.assertEqual(run.call_args.kwargs["timeout_s"], 99.0)

    def test_a_timeout_is_not_no_case_outcome(self) -> None:
        with mock.patch.object(
            tc, "run_suite", side_effect=rc.SuiteTimeoutError("SUITE_TIMEOUT:vitest")
        ), self.assertRaises(rc.SuiteTimeoutError):
            cr._run_suite(Path("t"), {"a.test.ts": "x"}, gate=None)

    def test_other_environment_refusals_are_still_absorbed(self) -> None:
        with mock.patch.object(
            tc,
            "run_suite",
            side_effect=rc.SuiteEnvironmentError("SUITE_RUNNER_UNUSABLE:vitest"),
        ):
            run, refusal = cr._run_suite(Path("t"), {"a.test.ts": "x"}, gate=None)
        self.assertEqual(run["executed"], 0)
        self.assertIsNotNone(refusal)

    def test_a_timeout_at_the_base_is_not_an_absolution(self) -> None:
        # `_collect_at_base` returning False tells `measure_candidate` the fault
        # predates the candidate. A base run that did not finish measured
        # nothing about the base either, so it must reach the operator.
        with tempfile.TemporaryDirectory() as scratch, mock.patch.object(
            cr, "_review_tree"
        ), mock.patch.object(cr.tb, "verify_suite"), mock.patch.object(
            tc, "run_suite", side_effect=rc.SuiteTimeoutError("SUITE_TIMEOUT:pytest")
        ) as run, self.assertRaises(rc.SuiteTimeoutError):
            cr._collect_at_base(
                candidate_repo=Path(scratch),
                builder_base_sha="0" * 40,
                files={"tests/test_a.py": "x"},
                scratch_root=Path(scratch),
                lane_id="lane",
                input_digest="0" * 64,
                gate=None,
                provision_argv=(),
                provision_timeout_s=None,
                state_root=Path(scratch),
                suite_timeout_s=42.0,
            )
        self.assertEqual(run.call_args.kwargs["timeout_s"], 42.0)


class TheBudgetIsADeploymentKey(unittest.TestCase):
    def test_absent_defers_to_the_suite_default(self) -> None:
        self.assertIsNone(maestro._config_timeout(None, "suite_timeout_s", None))

    def test_absent_provisioning_timeout_keeps_its_default(self) -> None:
        self.assertEqual(
            maestro._config_timeout(None, "provision_timeout_s"),
            lch.PROVISION_TIMEOUT_S,
        )

    def test_a_value_is_validated_like_the_provisioning_timeout(self) -> None:
        self.assertEqual(maestro._config_timeout(7200, "suite_timeout_s", None), 7200.0)
        for bad in (0, -1, "600", True):
            with self.subTest(value=bad), self.assertRaises(
                maestro._MaestroConfigurationError
            ):
                maestro._config_timeout(bad, "suite_timeout_s", None)

    def test_the_scheduler_reads_it_from_the_launcher(self) -> None:
        actor = SimpleNamespace(launcher=SimpleNamespace(suite_timeout_s=7200))
        self.assertEqual(sch._resolved_suite_timeout(actor), 7200.0)
        self.assertIsNone(sch._resolved_suite_timeout(SimpleNamespace()))
        self.assertIsNone(
            sch._resolved_suite_timeout(
                SimpleNamespace(launcher=SimpleNamespace(suite_timeout_s=None))
            )
        )

    def test_every_scheduler_suite_call_carries_the_budget(self) -> None:
        # The suite runs from three scheduler sites (candidate measurement,
        # review binding, integration gate). Each passes the provisioning bound,
        # and a site that drops the suite bound falls back to the default
        # silently, so every call carrying one must carry the other.
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(sch))
        sites = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            names = {kw.arg for kw in node.keywords}
            # Remote runs that are not the suite (preflight probe, draft
            # collection, probes) carry their own bound, not the suite's.
            if ast.unparse(node.func) in ("rs.call", "cr.measure_probes"):
                continue
            if "provision_timeout_s" in names and any(
                isinstance(kw.value, ast.Attribute)
                and kw.value.attr == "_provision_timeout_s"
                for kw in node.keywords
            ):
                sites += 1
                self.assertIn("suite_timeout_s", names, ast.unparse(node.func))
        self.assertEqual(sites, 3)

    def test_the_launcher_carries_the_key(self) -> None:
        self.assertIn("suite_timeout_s", lch.HerdrLauncher.__init__.__code__.co_varnames)


if __name__ == "__main__":
    unittest.main()
