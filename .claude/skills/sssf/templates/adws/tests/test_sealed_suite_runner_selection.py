"""The sealed suite's runner is derived from the sealed files, never assumed.

Nothing in the artifact-factory plan schema can carry a gate, so every sealed
suite reaches `tests_chain.run_private_suite` with `gate=None`. That path used
to answer `pytest` unconditionally and pin the invocation to the scheduler's own
interpreter, so a vitest suite was executed by pytest (`found no collectors`,
exit 4, zero cases) and a pytest suite was executed by whatever Python the
scheduler happened to run under.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adw_modules import private_review as pr
from adw_modules import runner_resolution as rr
from adw_modules import tests_chain as tc


class DerivedRunnerTest(unittest.TestCase):
    def test_typescript_test_files_derive_vitest(self) -> None:
        for files in (
            ("src/lib/seo/paid-dpa.test.ts",),
            ("src/a.test.tsx", "src/b.spec.ts"),
            ("src/a.test.js", "src/b.spec.mjs", "src/c.test.jsx"),
        ):
            with self.subTest(files=files):
                self.assertEqual(tc._suite_gate(None, files).runner, "vitest")

    def test_python_files_derive_pytest(self) -> None:
        for files in (
            ("tests/test_thing.py",),
            ("tests/test_thing.py", "tests/conftest.py"),
        ):
            with self.subTest(files=files):
                self.assertEqual(tc._suite_gate(None, files).runner, "pytest")

    def test_non_test_javascript_helper_does_not_vote(self) -> None:
        gate = tc._suite_gate(None, ("src/a.test.ts", "src/helpers.ts"))
        self.assertEqual(gate.runner, "vitest")
        self.assertEqual(gate.argv, ("src/a.test.ts", "src/helpers.ts"))

    def test_mixed_runners_refuse(self) -> None:
        with self.assertRaises(pr.PrivateReviewError) as caught:
            tc._suite_gate(None, ("tests/test_thing.py", "src/a.test.ts"))
        self.assertIn("SEALED_SUITE_RUNNER_AMBIGUOUS", str(caught.exception))

    def test_unrecognised_files_refuse(self) -> None:
        for files in ((), ("src/helpers.ts",), ("docs/notes.md", "Makefile")):
            with self.subTest(files=files):
                with self.assertRaises(pr.PrivateReviewError) as caught:
                    tc._suite_gate(None, files)
                self.assertIn(
                    "SEALED_SUITE_RUNNER_UNDERIVABLE", str(caught.exception)
                )

    def test_refusal_does_not_name_sealed_files(self) -> None:
        with self.assertRaises(pr.PrivateReviewError) as caught:
            tc._suite_gate(None, ("private/secret_selector.ts",))
        self.assertNotIn("secret_selector", str(caught.exception))

    def test_derived_defaults_are_unchanged(self) -> None:
        gate = tc._suite_gate(None, ("tests/test_thing.py",))
        self.assertEqual(gate.cwd, ".")
        self.assertEqual(gate.min_cases, 1)
        self.assertEqual(gate.argv, ("tests/test_thing.py",))


class ExplicitGateStillWinsTest(unittest.TestCase):
    def test_namespace_gate_is_returned_untouched(self) -> None:
        gate = SimpleNamespace(
            runner="pytest", argv=("-x", "app/t.py"), cwd="pkg", min_cases=7
        )
        self.assertIs(tc._suite_gate(gate, ("src/a.test.ts",)), gate)

    def test_mapping_gate_overrides_the_derivation(self) -> None:
        bound = tc._suite_gate(
            {"runner": "pytest", "argv": ["app/t.py"], "cwd": "pkg", "min_cases": 3},
            ("src/a.test.ts",),
        )
        self.assertEqual(bound.runner, "pytest")
        self.assertEqual(bound.argv, ("app/t.py",))
        self.assertEqual(bound.cwd, "pkg")
        self.assertEqual(bound.min_cases, 3)

    def test_mapping_gate_validation_is_unchanged(self) -> None:
        for gate, fragment in (
            ({"runner": "nose", "min_cases": 1}, "unsupported sealed suite runner"),
            ({"runner": "pytest", "min_cases": 0}, "min_cases"),
            ({"runner": "pytest", "min_cases": True}, "min_cases"),
            ({"runner": "pytest", "min_cases": 1, "argv": "x"}, "argv"),
            ({"runner": "pytest", "min_cases": 1, "cwd": 3}, "cwd"),
        ):
            with self.subTest(gate=gate):
                with self.assertRaises(pr.PrivateReviewError) as caught:
                    tc._suite_gate(gate, ("tests/test_thing.py",))
                self.assertIn(fragment, str(caught.exception))

    def test_a_non_mapping_gate_is_still_refused(self) -> None:
        with self.assertRaises(pr.PrivateReviewError) as caught:
            tc._suite_gate(["pytest"], ("tests/test_thing.py",))
        self.assertIn("not a mapping", str(caught.exception))


class PytestGoesThroughResolveTest(unittest.TestCase):
    """pytest is resolved on the same footing as vitest.

    The scheduler's own interpreter is not the project's. `rr.resolve` probes
    `.venv/bin/pytest`, `uv run pytest`, `poetry run pytest`, then `PATH`, and
    refuses when none is capable — a pin to `sys.executable` asks none of that.
    """

    _SUMMARY = {
        "pytest": "1 passed in 0.01s",
        "vitest": "\n Test Files  1 passed (1)\n      Tests  1 passed (1)\n",
    }

    def _run(self, files: tuple[str, ...], resolved: rr.ResolvedRunner) -> dict:
        with tempfile.TemporaryDirectory() as tree:
            with mock.patch.object(
                rr, "resolve", return_value=resolved
            ) as resolve, mock.patch.object(
                rr,
                "execute_cases",
                return_value={
                    "output": self._SUMMARY[resolved.runner],
                    "returncode": 0,
                },
            ) as execute:
                out = tc.run_private_suite(Path(tree), files)
            return {"out": out, "resolve": resolve, "execute": execute}

    def test_a_pytest_suite_is_resolved_not_pinned(self) -> None:
        resolved = rr.ResolvedRunner(
            runner="pytest",
            executable="/repo/.venv/bin/pytest",
            origin="discovered",
            probe_exit=5,
        )
        seen = self._run(("tests/test_thing.py",), resolved)
        seen["resolve"].assert_called_once()
        self.assertEqual(seen["resolve"].call_args.args[0], "pytest")
        used = seen["execute"].call_args.args[0]
        self.assertIs(used, resolved)
        self.assertEqual(used.executable, "/repo/.venv/bin/pytest")
        self.assertEqual(used.launcher_args, ())
        self.assertEqual(seen["out"]["runner"], "pytest")

    def test_a_vitest_suite_is_resolved_as_vitest(self) -> None:
        resolved = rr.ResolvedRunner(
            runner="vitest",
            executable="/repo/node_modules/.bin/vitest",
            origin="discovered",
        )
        seen = self._run(("src/a.test.ts",), resolved)
        self.assertEqual(seen["resolve"].call_args.args[0], "vitest")
        self.assertEqual(seen["out"]["runner"], "vitest")

    def test_an_unusable_pytest_refuses_rather_than_falling_back(self) -> None:
        unusable = rr.RunnerUnusable("pytest", rr.Reason.UNRESOLVED, ".")
        with tempfile.TemporaryDirectory() as tree:
            with mock.patch.object(rr, "resolve", side_effect=unusable):
                with self.assertRaises(pr.PrivateReviewError) as caught:
                    tc.run_private_suite(Path(tree), ("tests/test_thing.py",))
        self.assertIn("SEALED_SUITE_RUNNER_UNUSABLE:pytest", str(caught.exception))

    def test_pytest_has_a_measured_capability_probe(self) -> None:
        # `rr.resolve` refuses any runner without one, so routing pytest through
        # it would be a hard refusal if these rows were missing.
        self.assertIn("pytest", rr.PROBE_ARGS)
        self.assertIn("pytest", rr.CAPABLE_EXIT)
        self.assertTrue(rr._measured("pytest"))


class VersionSpecifierTest(unittest.TestCase):
    """The PEP 440 subset, measured against the cases that actually occur."""

    def test_satisfied(self) -> None:
        for version, specifier in (
            ((3, 12, 1), ">=3.12"),
            ((3, 12, 0), ">=3.12"),
            ((3, 13, 0), ">=3.12,<4.0"),
            ((3, 11, 9), "~=3.11"),
            ((3, 11, 9), "~=3.11.2"),
            ((3, 12, 4), "==3.12.*"),
            ((3, 10, 0), "!=3.9.*"),
            ((3, 12, 0), "==3.12.0"),
            ((3, 9, 6), "<=3.9.6"),
        ):
            with self.subTest(version=version, specifier=specifier):
                self.assertIs(tc._satisfies(version, specifier), True)

    def test_unsatisfied(self) -> None:
        for version, specifier in (
            ((3, 9, 6), ">=3.12"),
            ((3, 11, 13), ">=3.12"),
            ((4, 0, 0), ">=3.12,<4.0"),
            ((3, 12, 0), "~=3.11.2"),
            ((3, 11, 0), "==3.12.*"),
            ((3, 9, 7), "!=3.9.*"),
        ):
            with self.subTest(version=version, specifier=specifier):
                self.assertIs(tc._satisfies(version, specifier), False)

    def test_unreadable_specifiers_are_none_not_a_refusal(self) -> None:
        for specifier in ("=== 3.12", "at least 3.12", ">=3.12,~=", ">3.*"):
            with self.subTest(specifier=specifier):
                self.assertIsNone(tc._satisfies((3, 12, 0), specifier))


class InterpreterDiscoveryTest(unittest.TestCase):
    def test_a_launcher_swaps_the_runner_for_python(self) -> None:
        resolved = rr.ResolvedRunner(
            runner="pytest", executable="/bin/uv", launcher_args=("run", "pytest")
        )
        self.assertEqual(
            tc._interpreter_argv(resolved), ("/bin/uv", "run", "python")
        )

    def test_a_sibling_interpreter_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / "bin"
            binary.mkdir()
            script = binary / "pytest"
            script.write_text("#!/nonexistent/python\n", encoding="utf-8")
            script.chmod(0o755)
            python = binary / "python"
            python.write_text("", encoding="utf-8")
            python.chmod(0o755)
            resolved = rr.ResolvedRunner(runner="pytest", executable=str(script))
            self.assertEqual(tc._interpreter_argv(resolved), (str(python),))

    def test_the_shebang_answers_when_no_sibling_exists(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            elsewhere = Path(root) / "python3.12"
            elsewhere.write_text("", encoding="utf-8")
            elsewhere.chmod(0o755)
            script = Path(root) / "scripts" / "pytest"
            script.parent.mkdir()
            script.write_text("#!{0}\n".format(elsewhere), encoding="utf-8")
            script.chmod(0o755)
            resolved = rr.ResolvedRunner(runner="pytest", executable=str(script))
            self.assertEqual(tc._interpreter_argv(resolved), (str(elsewhere),))

    def test_an_unidentifiable_interpreter_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / "pytest"
            script.write_text("not a script\n", encoding="utf-8")
            script.chmod(0o755)
            resolved = rr.ResolvedRunner(runner="pytest", executable=str(script))
            self.assertIsNone(tc._interpreter_argv(resolved))


class RequiresPythonDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        if tc.tomllib is None:
            self.skipTest("tomllib is unavailable on this interpreter")

    def _repo(self, root: str) -> Path:
        service = Path(root) / "services" / "api-gateway"
        (service / "tests").mkdir(parents=True)
        (service / "pyproject.toml").write_text(
            '[project]\nname = "api-gateway"\nrequires-python = ">=3.12"\n',
            encoding="utf-8",
        )
        return Path(root)

    def test_a_nested_project_is_found_from_a_sealed_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root)
            found = tc._python_requirements(
                repo, ".", ("services/api-gateway/tests/test_x.py",)
            )
            expected = (repo / "services" / "api-gateway" / "pyproject.toml").resolve()
            self.assertEqual(found, ((str(expected), ">=3.12"),))

    def test_a_project_without_requires_python_declares_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "pyproject.toml").write_text(
                '[project]\nname = "x"\n', encoding="utf-8"
            )
            self.assertEqual(tc._python_requirements(Path(root), ".", ()), ())

    def test_the_walk_stops_at_the_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            (Path(outer) / "pyproject.toml").write_text(
                '[project]\nname = "outer"\nrequires-python = ">=3.99"\n',
                encoding="utf-8",
            )
            repo = Path(outer) / "repo"
            repo.mkdir()
            self.assertEqual(tc._python_requirements(repo, ".", ()), ())


class DeclaredPythonIsEnforcedTest(unittest.TestCase):
    """An interpreter the project refuses is refused here, by name."""

    def setUp(self) -> None:
        if tc.tomllib is None:
            self.skipTest("tomllib is unavailable on this interpreter")

    def _tree(self, stack: contextlib.ExitStack) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        (root / "tests").mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "gateway"\nrequires-python = ">=3.12"\n',
            encoding="utf-8",
        )
        return root

    def _resolved(self) -> rr.ResolvedRunner:
        return rr.ResolvedRunner(
            runner="pytest", executable="/repo/.venv/bin/pytest", origin="discovered"
        )

    def test_an_unsatisfying_interpreter_is_refused_and_names_the_mismatch(
        self,
    ) -> None:
        with contextlib.ExitStack() as stack:
            root = self._tree(stack)
            resolved = self._resolved()
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_argv", return_value=("/py39",))
            )
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_release", return_value=(3, 9, 6))
            )
            with self.assertRaises(pr.PrivateReviewError) as caught:
                tc._assert_declared_python(
                    resolved, root, root, ".", ("tests/test_x.py",)
                )
        message = str(caught.exception)
        self.assertIn("SEALED_SUITE_PYTHON_UNSUPPORTED", message)
        self.assertIn("3.9.6", message)
        self.assertIn(">=3.12", message)
        self.assertIn("pyproject.toml", message)
        self.assertIn("harness environment fault", message)

    def test_a_satisfying_interpreter_is_accepted(self) -> None:
        with contextlib.ExitStack() as stack:
            root = self._tree(stack)
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_argv", return_value=("/py312",))
            )
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_release", return_value=(3, 12, 4))
            )
            self.assertIsNone(
                tc._assert_declared_python(
                    self._resolved(), root, root, ".", ("tests/test_x.py",)
                )
            )

    def test_vitest_is_never_version_checked(self) -> None:
        with contextlib.ExitStack() as stack:
            root = self._tree(stack)
            argv = stack.enter_context(mock.patch.object(tc, "_interpreter_argv"))
            resolved = rr.ResolvedRunner(runner="vitest", executable="/bin/vitest")
            tc._assert_declared_python(resolved, root, root, ".", ("src/a.test.ts",))
            argv.assert_not_called()

    def test_an_unmeasurable_interpreter_degrades_rather_than_refusing(self) -> None:
        with contextlib.ExitStack() as stack:
            root = self._tree(stack)
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_argv", return_value=None)
            )
            self.assertIsNone(
                tc._assert_declared_python(
                    self._resolved(), root, root, ".", ("tests/test_x.py",)
                )
            )

    def test_run_private_suite_refuses_before_it_executes_anything(self) -> None:
        with contextlib.ExitStack() as stack:
            root = self._tree(stack)
            (root / "tests" / "test_x.py").write_text("", encoding="utf-8")
            stack.enter_context(
                mock.patch.object(
                    rr,
                    "resolve",
                    return_value=rr.ResolvedRunner(
                        runner="pytest", executable="/repo/.venv/bin/pytest"
                    ),
                )
            )
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_argv", return_value=("/py39",))
            )
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_release", return_value=(3, 9, 6))
            )
            execute = stack.enter_context(mock.patch.object(rr, "execute_cases"))
            with self.assertRaises(pr.PrivateReviewError) as caught:
                tc.run_private_suite(root, ("tests/test_x.py",))
            execute.assert_not_called()
        self.assertIn("SEALED_SUITE_PYTHON_UNSUPPORTED", str(caught.exception))


class MeasuredAgainstARealInterpreterTest(unittest.TestCase):
    """`_interpreter_release` runs a real binary; a stub cannot prove it works."""

    def test_this_interpreter_reports_its_own_version(self) -> None:
        release = tc._interpreter_release((sys.executable,), Path.cwd())
        self.assertEqual(release, tuple(sys.version_info[:3]))

    def test_a_binary_that_is_not_an_interpreter_reports_nothing(self) -> None:
        self.assertIsNone(tc._interpreter_release(("/bin/echo",), Path.cwd()))
        self.assertIsNone(
            tc._interpreter_release(("/nonexistent/python",), Path.cwd())
        )


def _real_pytest() -> rr.ResolvedRunner:
    """The interpreter running this suite, invoking its own pytest for real."""
    return rr.ResolvedRunner(
        runner="pytest",
        executable=sys.executable,
        launcher_args=("-m", "pytest"),
        origin="declared",
    )


def _discover_vitest() -> str | None:
    declared = os.environ.get("MAESTRO_TEST_VITEST")
    if declared and os.access(declared, os.X_OK):
        return declared
    return shutil.which("vitest")


class UnparseableCountsAreRefusedTest(unittest.TestCase):
    """Exit 0 with nothing counted is an unreadable measurement, not a pass."""

    def _run(self, files: tuple[str, ...], runner: str, output: str) -> str:
        with tempfile.TemporaryDirectory() as tree:
            with mock.patch.object(
                rr,
                "resolve",
                return_value=rr.ResolvedRunner(runner=runner, executable="/bin/x"),
            ), mock.patch.object(
                rr,
                "execute_cases",
                return_value={"output": output, "returncode": 0},
            ):
                with self.assertRaises(pr.PrivateReviewError) as caught:
                    tc.run_private_suite(Path(tree), files)
        return str(caught.exception)

    def test_vitest_with_no_summary_is_refused(self) -> None:
        message = self._run(("src/a.test.ts",), "vitest", "\n RUN  v4.1.11\n\n")
        self.assertIn("SEALED_SUITE_COUNTS_UNPARSEABLE:vitest", message)

    def test_pytest_with_no_summary_is_refused(self) -> None:
        message = self._run(("tests/test_a.py",), "pytest", "no summary here\n")
        self.assertIn("SEALED_SUITE_COUNTS_UNPARSEABLE:pytest", message)

    def test_the_refusal_names_the_measurement_not_the_candidate(self) -> None:
        message = self._run(("tests/test_a.py",), "pytest", "")
        self.assertIn("could not be measured", message)
        self.assertIn("not a defect in the candidate", message)

    def test_the_refusal_carries_no_sealed_file_name(self) -> None:
        message = self._run(("tests/test_secret_selector.py",), "pytest", "")
        self.assertNotIn("secret_selector", message)
        self.assertNotIn("tests/", message)

    def test_a_nonzero_exit_is_a_failure_not_a_refusal(self) -> None:
        # `code_review` already reads a non-zero return code as a failed suite.
        # Only the exit-0 case is ambiguous, so only it refuses.
        with tempfile.TemporaryDirectory() as tree:
            with mock.patch.object(
                rr,
                "resolve",
                return_value=rr.ResolvedRunner(runner="vitest", executable="/bin/x"),
            ), mock.patch.object(
                rr,
                "execute_cases",
                return_value={"output": "No test files found\n", "returncode": 1},
            ):
                out = tc.run_private_suite(Path(tree), ("src/a.test.ts",))
        self.assertEqual(out["executed"], 0)
        self.assertEqual(out["returncode"], 1)
        self.assertEqual(out["counts"]["passed"], 0)

    def test_no_count_is_ever_fabricated(self) -> None:
        with tempfile.TemporaryDirectory() as tree:
            with mock.patch.object(
                rr,
                "resolve",
                return_value=rr.ResolvedRunner(runner="vitest", executable="/bin/x"),
            ), mock.patch.object(
                rr,
                "execute_cases",
                return_value={
                    "output": "\n Test Files  1 passed (1)\n      Tests  3 passed (3)\n",
                    "returncode": 0,
                },
            ):
                out = tc.run_private_suite(Path(tree), ("src/a.test.ts",))
        self.assertEqual(out["executed"], 3)
        self.assertEqual(out["counts"]["passed"], 3)
        self.assertEqual(out["min_cases"], 1)


class RealPytestIsCountedTest(unittest.TestCase):
    """Run the real pytest. A stubbed `subprocess.run` replays scripted stdout
    and can say nothing about what a runner actually prints."""

    @contextlib.contextmanager
    def _tree(self, name: str, body: str):
        with tempfile.TemporaryDirectory() as root:
            tests = Path(root) / "tests"
            tests.mkdir()
            (tests / name).write_text(body, encoding="utf-8")
            with mock.patch.object(rr, "resolve", return_value=_real_pytest()):
                yield Path(root), "tests/{0}".format(name)

    def test_passing_cases_are_counted_from_real_output(self) -> None:
        body = "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n"
        with self._tree("test_real.py", body) as (root, selector):
            out = tc.run_private_suite(root, (selector,))
        self.assertEqual(out["returncode"], 0)
        self.assertEqual(out["executed"], 2)
        self.assertEqual(out["counts"]["passed"], 2)

    def test_failing_cases_are_counted_from_real_output(self) -> None:
        body = "def test_one():\n    assert True\n\n\ndef test_two():\n    assert False\n"
        with self._tree("test_real_fail.py", body) as (root, selector):
            out = tc.run_private_suite(root, (selector,))
        self.assertNotEqual(out["returncode"], 0)
        self.assertEqual(out["executed"], 2)
        self.assertEqual(out["counts"]["failed"], 1)

    def test_skipped_cases_are_counted_beside_a_case_that_ran(self) -> None:
        body = (
            "import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_one():\n"
            "    assert True\n\n\ndef test_two():\n    assert True\n"
        )
        with self._tree("test_real_skip.py", body) as (root, selector):
            out = tc.run_private_suite(root, (selector,))
        self.assertEqual(out["returncode"], 0)
        self.assertEqual(out["counts"]["skipped"], 1)
        self.assertEqual(out["counts"]["passed"], 1)
        self.assertEqual(out["executed"], 2)

    def test_errored_collection_stays_on_the_failure_path(self) -> None:
        body = "import a_module_that_does_not_exist  # noqa: F401\n"
        with self._tree("test_real_error.py", body) as (root, selector):
            out = tc.run_private_suite(root, (selector,))
        self.assertNotEqual(out["returncode"], 0)
        self.assertEqual(out["counts"]["errored"], 1)
        self.assertEqual(out["executed"], 1)


class RealVitestIsCountedTest(unittest.TestCase):
    """The same proof for vitest, against a real vitest binary when one exists.

    Skips rather than stubs. A fake here would assert the shape of output this
    file already assumes, which is the assumption under test.
    """

    def setUp(self) -> None:
        self.vitest = _discover_vitest()
        if self.vitest is None:
            self.skipTest("no vitest binary found; set MAESTRO_TEST_VITEST")

    def _project(self, stack: contextlib.ExitStack) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        (root / "package.json").write_text(
            '{"name":"sealed","private":true,"type":"module"}\n', encoding="utf-8"
        )
        (root / "src").mkdir()
        return root

    def test_real_cases_are_counted_and_the_file_is_not_touched(self) -> None:
        source = (
            'import { it, expect } from "vitest";\n'
            'it("adds", () => { expect(1 + 1).toBe(2); });\n'
            'it("concats", () => { expect("a" + "b").toBe("ab"); });\n'
        )
        with contextlib.ExitStack() as stack:
            root = self._project(stack)
            case = root / "src" / "a.test.ts"
            case.write_text(source, encoding="utf-8")
            before = hashlib.sha256(case.read_bytes()).hexdigest()
            stack.enter_context(
                mock.patch.object(
                    rr,
                    "resolve",
                    return_value=rr.ResolvedRunner(
                        runner="vitest", executable=self.vitest
                    ),
                )
            )
            out = tc.run_private_suite(root, ("src/a.test.ts",), timeout_s=300.0)
            after = hashlib.sha256(case.read_bytes()).hexdigest()
        # A measurement must never mutate its subject: `vitest list --json <path>`
        # once overwrote a tester's committed test file, because `--json` takes
        # an optional value and read the path as its destination.
        self.assertEqual(before, after)
        self.assertEqual(out["runner"], "vitest")
        self.assertEqual(out["returncode"], 0)
        self.assertEqual(out["executed"], 2)
        self.assertEqual(out["counts"]["passed"], 2)


#: The real shape, captured from `rr.execute_cases` against vitest 4.1.11 and
#: 3.2.7. The summary is on stdout and the failure banner is on stderr, and
#: `execute_cases` returns `stdout + "\n" + stderr`, so the banner is ALWAYS
#: last on a failing run whatever order it was printed in. Kept as a literal so
#: the regression is pinned on machines with no vitest installed.
_VITEST_FAILING_OUTPUT = """
 RUN  v4.1.11 /tmp/vfix

 ❯ src/allfail.test.ts (3 tests | 3 failed) 4ms

 Test Files  1 failed (1)
      Tests  3 failed (3)
   Start at  17:20:01
   Duration  71ms

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 3 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/allfail.test.ts > a
AssertionError: expected 1 to be 2
"""

_VITEST_MIXED_OUTPUT = """
 Test Files  1 failed (1)
      Tests  1 failed | 1 passed (2)

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯
"""


class VitestSummaryParsingTest(unittest.TestCase):
    """The totals line is found by its shape, not by containing `Tests`.

    Scanning backwards for the last line containing the word `Tests` found the
    failure banner instead, which carries no count, so every failing vitest
    suite parsed to all zeros.
    """

    def test_the_failure_banner_does_not_defeat_the_summary(self) -> None:
        counts = tc._parse_suite_counts("vitest", _VITEST_FAILING_OUTPUT)
        self.assertEqual(counts["failed"], 3)
        self.assertEqual(sum(counts.values()), 3)

    def test_a_mixed_run_counts_both_sides_past_the_banner(self) -> None:
        counts = tc._parse_suite_counts("vitest", _VITEST_MIXED_OUTPUT)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["passed"], 1)

    def test_the_banner_alone_parses_to_nothing(self) -> None:
        # Not a summary, so it must contribute no counts rather than be read as
        # one. `run_private_suite` turns a zero count on exit 0 into a refusal.
        banner = "⎯⎯⎯ Failed Tests 11 ⎯⎯⎯"
        self.assertEqual(sum(tc._parse_suite_counts("vitest", banner).values()), 0)

    def test_the_test_files_line_is_not_the_summary(self) -> None:
        # `Test Files  1 failed (1)` counts FILES. Reading it as the case
        # summary would report 1 failed case for a file holding eleven.
        output = " Test Files  1 failed (1)\n      Tests  11 failed (11)\n"
        self.assertEqual(tc._parse_suite_counts("vitest", output)["failed"], 11)
        files_only = " Test Files  1 failed (1)\n"
        self.assertEqual(sum(tc._parse_suite_counts("vitest", files_only).values()), 0)

    def test_the_summary_anchor_tolerates_the_reporters_indentation(self) -> None:
        for line in ("      Tests  2 passed (2)", "Tests  2 passed (2)"):
            with self.subTest(line=line):
                self.assertEqual(
                    tc._parse_suite_counts("vitest", line)["passed"], 2
                )

    def test_pytest_parsing_is_untouched_by_the_vitest_anchor(self) -> None:
        # The two branches are independent; the anchor is vitest's alone.
        output = (
            "1 failed, 1 passed in 0.01s\n"
            "⎯⎯⎯ Failed Tests 1 ⎯⎯⎯\n"
        )
        counts = tc._parse_suite_counts("pytest", output)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["passed"], 1)


class RealVitestCountsEveryShapeTest(unittest.TestCase):
    """All four shapes, real binary, through `rr.execute_cases`.

    A shell run with `2>&1` interleaves stdout and stderr in real time and puts
    the banner FIRST, which parses correctly and hides the defect entirely.
    Only the harness's separate capture reorders them, so the measurement has
    to go through `execute_cases` rather than through a terminal.
    """

    def setUp(self) -> None:
        self.vitest = _discover_vitest()
        if self.vitest is None:
            self.skipTest("no vitest binary found; set MAESTRO_TEST_VITEST")

    @contextlib.contextmanager
    def _project(self, body: str):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "package.json").write_text(
                '{"name":"sealed","private":true,"type":"module"}\n', encoding="utf-8"
            )
            (root / "src").mkdir()
            (root / "src" / "a.test.ts").write_text(body, encoding="utf-8")
            yield root, "src/a.test.ts"

    def _counts(self, body: str) -> tuple[dict, int]:
        with self._project(body) as (root, selector):
            resolved = rr.ResolvedRunner(runner="vitest", executable=self.vitest)
            gate = tc._suite_gate(None, (selector,))
            exec_gate = SimpleNamespace(
                runner="vitest",
                argv=tc._suite_selectors(gate, (selector,)),
                cwd=".",
                min_cases=1,
            )
            raw = rr.execute_cases(resolved, exec_gate, root, timeout_s=300.0)
        return tc._parse_suite_counts("vitest", raw["output"]), int(raw["returncode"])

    def test_all_passing(self) -> None:
        counts, code = self._counts(
            'import { it, expect } from "vitest";\n'
            'it("a", () => { expect(1).toBe(1); });\n'
            'it("b", () => { expect(1).toBe(1); });\n'
        )
        self.assertEqual(code, 0)
        self.assertEqual(counts["passed"], 2)

    def test_all_failing_reports_the_true_count_not_zero(self) -> None:
        counts, code = self._counts(
            'import { it, expect } from "vitest";\n'
            'it("a", () => { expect(1).toBe(2); });\n'
            'it("b", () => { expect(1).toBe(2); });\n'
            'it("c", () => { expect(1).toBe(2); });\n'
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(counts["failed"], 3)
        self.assertEqual(sum(counts.values()), 3)

    def test_mixed_reports_both_sides(self) -> None:
        counts, code = self._counts(
            'import { it, expect } from "vitest";\n'
            'it("ok", () => { expect(1).toBe(1); });\n'
            'it("bad", () => { expect(1).toBe(2); });\n'
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(counts["passed"], 1)
        self.assertEqual(counts["failed"], 1)

    def test_all_skipped_is_counted_then_refused_as_unevaluated(self) -> None:
        body = (
            'import { it, expect } from "vitest";\n'
            'it.skip("a", () => { expect(1).toBe(1); });\n'
            'it.skip("b", () => { expect(1).toBe(1); });\n'
        )
        counts, code = self._counts(body)
        self.assertEqual(code, 0)
        self.assertEqual(counts["skipped"], 2)
        # Reaching ALL_CASES_SKIPPED rather than COUNTS_UNPARSEABLE is itself
        # proof the summary parsed: an unread summary would have measured zero.
        with self._project(body) as (root, selector):
            with mock.patch.object(
                rr,
                "resolve",
                return_value=rr.ResolvedRunner(runner="vitest", executable=self.vitest),
            ):
                with self.assertRaises(pr.SealedEnvironmentError) as caught:
                    tc.run_private_suite(root, (selector,), timeout_s=300.0)
        message = str(caught.exception)
        self.assertIn("SEALED_SUITE_ALL_CASES_SKIPPED:vitest", message)
        self.assertIn("every one of the 2 counted cases", message)

    def test_the_summary_is_on_stdout_and_the_banner_on_stderr(self) -> None:
        """The mechanism itself, so a reporter change is caught here first."""
        with self._project(
            'import { it, expect } from "vitest";\n'
            'it("bad", () => { expect(1).toBe(2); });\n'
        ) as (root, selector):
            result = subprocess.run(
                [self.vitest, "run", selector],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=300.0,
            )
        summary = [
            line for line in result.stdout.splitlines() if tc._VITEST_SUMMARY.match(line)
        ]
        self.assertEqual(len(summary), 1, result.stdout)
        self.assertIn("Failed Tests", result.stderr)
        self.assertNotIn("Failed Tests", result.stdout)


class EnvironmentFaultsAreTypedTest(unittest.TestCase):
    """Every environment fault is a `pr.SealedEnvironmentError` by class.

    The operator boundary recognises these structurally. A match on the message
    prefix would rot the first time a code is renamed or a sixth case is added:
    recognition would fail silently and the operator would get a traceback
    instead of a repair instruction.
    """

    def _refuse(self, call) -> pr.PrivateReviewError:
        with self.assertRaises(pr.PrivateReviewError) as caught:
            call()
        return caught.exception

    def _suite(self, files: tuple[str, ...], runner: str, raw: dict):
        def call() -> None:
            with tempfile.TemporaryDirectory() as tree:
                with mock.patch.object(
                    rr,
                    "resolve",
                    return_value=rr.ResolvedRunner(runner=runner, executable="/bin/x"),
                ), mock.patch.object(rr, "execute_cases", return_value=raw):
                    tc.run_private_suite(Path(tree), files)

        return call

    def test_every_environment_fault_subclasses_the_shared_base(self) -> None:
        cases = {
            "SEALED_SUITE_RUNNER_UNDERIVABLE": lambda: tc._suite_gate(
                None, ("src/helpers.ts",)
            ),
            "SEALED_SUITE_RUNNER_AMBIGUOUS": lambda: tc._suite_gate(
                None, ("tests/test_a.py", "src/a.test.ts")
            ),
            "SEALED_SUITE_COUNTS_UNPARSEABLE": self._suite(
                ("tests/test_a.py",), "pytest", {"output": "", "returncode": 0}
            ),
            "SEALED_SUITE_ALL_CASES_SKIPPED": self._suite(
                ("tests/test_a.py",),
                "pytest",
                {"output": "2 skipped in 0.01s", "returncode": 0},
            ),
        }
        for code, call in cases.items():
            with self.subTest(code=code):
                exc = self._refuse(call)
                self.assertIsInstance(exc, pr.SealedEnvironmentError)
                self.assertTrue(str(exc).startswith(code), str(exc))

    def test_an_unusable_runner_is_typed(self) -> None:
        def call() -> None:
            with tempfile.TemporaryDirectory() as tree:
                with mock.patch.object(
                    rr,
                    "resolve",
                    side_effect=rr.RunnerUnusable(
                        "pytest", rr.Reason.UNRESOLVED, "."
                    ),
                ):
                    tc.run_private_suite(Path(tree), ("tests/test_a.py",))

        exc = self._refuse(call)
        self.assertIsInstance(exc, pr.SealedEnvironmentError)
        self.assertTrue(str(exc).startswith("SEALED_SUITE_RUNNER_UNUSABLE:pytest"))

    def test_an_unsupported_interpreter_is_typed(self) -> None:
        if tc.tomllib is None:
            self.skipTest("tomllib is unavailable on this interpreter")
        with contextlib.ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            (root / "tests").mkdir()
            (root / "pyproject.toml").write_text(
                '[project]\nname = "c"\nrequires-python = ">=3.12"\n', encoding="utf-8"
            )
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_argv", return_value=("/py39",))
            )
            stack.enter_context(
                mock.patch.object(tc, "_interpreter_release", return_value=(3, 9, 6))
            )
            resolved = rr.ResolvedRunner(runner="pytest", executable="/venv/bin/pytest")
            exc = self._refuse(
                lambda: tc._assert_declared_python(
                    resolved, root, root, ".", ("tests/test_x.py",)
                )
            )
        self.assertIsInstance(exc, pr.SealedEnvironmentError)
        self.assertTrue(str(exc).startswith("SEALED_SUITE_PYTHON_UNSUPPORTED"))

    def test_the_message_text_is_unchanged_by_the_reparenting(self) -> None:
        # worker-provision asserts the resolved invocation, measured version,
        # specifier and declaring file survive into the operator's detail. Only
        # the class moved.
        exc = self._refuse(
            self._suite(
                ("tests/test_a.py",),
                "pytest",
                {"output": "2 skipped in 0.01s", "returncode": 0},
            )
        )
        self.assertIn("every one of the 2 counted cases was skipped", str(exc))
        self.assertIn("not a defect in the candidate under test", str(exc))

    def test_the_base_carries_the_operator_outcome_code(self) -> None:
        self.assertEqual(
            pr.SealedEnvironmentError.code, "SEALED_SUITE_ENVIRONMENT_REFUSED"
        )

    def test_the_operator_boundary_recognises_every_code_this_module_raises(
        self,
    ) -> None:
        """The seam, pinned from the raising side.

        The boundary's own tests enumerate the codes it knew about when they
        were written. This asserts the reverse direction: every code this
        module raises reaches the operator as an environment fault. Recognition
        is by class, so a code added here needs no edit there — and this test
        is what catches it if that ever stops being true.
        """
        from adw_modules import code_review as cr

        codes = (
            "SEALED_SUITE_RUNNER_UNDERIVABLE",
            "SEALED_SUITE_RUNNER_AMBIGUOUS",
            "SEALED_SUITE_PYTHON_UNSUPPORTED",
            "SEALED_SUITE_RUNNER_UNUSABLE",
            "SEALED_SUITE_COUNTS_UNPARSEABLE",
            "SEALED_SUITE_ALL_CASES_SKIPPED",
        )
        for code in codes:
            with self.subTest(code=code):
                exc = pr.SealedEnvironmentError("{0}: detail".format(code))
                detail = cr.sealed_environment_detail(exc)
                self.assertIsNotNone(detail, code)
                self.assertIn("{0}: detail".format(code), detail)

    def test_a_contract_refusal_is_not_recognised_as_an_environment_fault(
        self,
    ) -> None:
        from adw_modules import code_review as cr

        self.assertIsNone(
            cr.sealed_environment_detail(
                pr.PrivateReviewError("sealed suite gate is not a mapping")
            )
        )

    def test_contract_refusals_are_not_environment_faults(self) -> None:
        # A factory invariant the operator cannot fix by installing anything
        # must never be typed as a machine fault, or the operator is sent to
        # repair an environment that is already correct.
        for gate, files in (
            (["pytest"], ("tests/test_a.py",)),
            ({"runner": "nose", "min_cases": 1}, ("tests/test_a.py",)),
            ({"runner": "pytest", "min_cases": 0}, ("tests/test_a.py",)),
        ):
            with self.subTest(gate=gate):
                exc = self._refuse(lambda: tc._suite_gate(gate, files))
                self.assertIsInstance(exc, pr.PrivateReviewError)
                self.assertNotIsInstance(exc, pr.SealedEnvironmentError)


class AllCasesSkippedIsRefusedTest(unittest.TestCase):
    """A suite where every counted case was skipped evaluated nothing.

    `executed` counts skips, so without this a fully skipped sealed suite
    clears `code_review`'s `executed < min_cases` check and binds a candidate
    green having asserted nothing — the same false green as a fabricated count,
    one layer down. Run against the real binaries, because whether a fully
    skipped suite exits 0 at all is a fact about the runners, not about this
    file.
    """

    @contextlib.contextmanager
    def _pytest_tree(self, name: str, body: str):
        with tempfile.TemporaryDirectory() as root:
            tests = Path(root) / "tests"
            tests.mkdir()
            (tests / name).write_text(body, encoding="utf-8")
            with mock.patch.object(rr, "resolve", return_value=_real_pytest()):
                yield Path(root), "tests/{0}".format(name)

    @contextlib.contextmanager
    def _vitest_tree(self, body: str):
        vitest = _discover_vitest()
        if vitest is None:
            self.skipTest("no vitest binary found; set MAESTRO_TEST_VITEST")
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "package.json").write_text(
                '{"name":"sealed","private":true,"type":"module"}\n', encoding="utf-8"
            )
            (root / "src").mkdir()
            (root / "src" / "a.test.ts").write_text(body, encoding="utf-8")
            resolved = rr.ResolvedRunner(runner="vitest", executable=vitest)
            with mock.patch.object(rr, "resolve", return_value=resolved):
                yield root, "src/a.test.ts"

    def test_a_fully_skipped_real_pytest_suite_is_refused(self) -> None:
        body = (
            "import pytest\n\n\n@pytest.mark.skip(reason='x')\ndef test_one():\n"
            "    assert True\n\n\n@pytest.mark.skip(reason='x')\ndef test_two():\n"
            "    assert True\n"
        )
        with self._pytest_tree("test_all_skipped.py", body) as (root, selector):
            with self.assertRaises(pr.PrivateReviewError) as caught:
                tc.run_private_suite(root, (selector,))
        message = str(caught.exception)
        self.assertIn("SEALED_SUITE_ALL_CASES_SKIPPED:pytest", message)
        self.assertIn("evaluated nothing", message)
        self.assertIn("not a defect in the candidate", message)

    def test_a_fully_skipped_real_vitest_suite_is_refused(self) -> None:
        body = (
            'import { it, expect } from "vitest";\n'
            'it.skip("a", () => { expect(1).toBe(1); });\n'
            'it.skip("b", () => { expect(1).toBe(1); });\n'
        )
        with self._vitest_tree(body) as (root, selector):
            with self.assertRaises(pr.PrivateReviewError) as caught:
                tc.run_private_suite(root, (selector,), timeout_s=300.0)
        self.assertIn("SEALED_SUITE_ALL_CASES_SKIPPED:vitest", str(caught.exception))

    def test_one_passed_beside_many_skipped_is_not_refused(self) -> None:
        body = ["import pytest\n\n"]
        for index in range(10):
            body.append(
                "\n@pytest.mark.skip(reason='x')\ndef test_s{0}():\n"
                "    assert True\n".format(index)
            )
        body.append("\ndef test_real():\n    assert 1 + 1 == 2\n")
        with self._pytest_tree("test_mixed.py", "".join(body)) as (root, selector):
            out = tc.run_private_suite(root, (selector,))
        self.assertEqual(out["returncode"], 0)
        self.assertEqual(out["counts"]["passed"], 1)
        self.assertEqual(out["counts"]["skipped"], 10)
        self.assertEqual(out["executed"], 11)

    def test_one_passed_beside_skipped_is_not_refused_in_vitest(self) -> None:
        body = (
            'import { it, expect } from "vitest";\n'
            'it.skip("a", () => { expect(1).toBe(1); });\n'
            'it("b", () => { expect(1 + 1).toBe(2); });\n'
        )
        with self._vitest_tree(body) as (root, selector):
            out = tc.run_private_suite(root, (selector,), timeout_s=300.0)
        self.assertEqual(out["returncode"], 0)
        self.assertEqual(out["counts"]["passed"], 1)
        self.assertEqual(out["counts"]["skipped"], 1)
        self.assertEqual(out["executed"], 2)

    def test_a_fully_failing_suite_stays_on_the_failure_path(self) -> None:
        body = (
            "def test_one():\n    assert False\n\n\n"
            "def test_two():\n    assert False\n"
        )
        with self._pytest_tree("test_all_fail.py", body) as (root, selector):
            out = tc.run_private_suite(root, (selector,))
        self.assertNotEqual(out["returncode"], 0)
        self.assertEqual(out["counts"]["failed"], 2)
        self.assertEqual(out["executed"], 2)

    def test_the_refusal_carries_no_sealed_file_name(self) -> None:
        body = (
            "import pytest\n\n\n@pytest.mark.skip(reason='x')\n"
            "def test_secret_selector():\n    assert True\n"
        )
        with self._pytest_tree("test_secret_selector.py", body) as (root, selector):
            with self.assertRaises(pr.PrivateReviewError) as caught:
                tc.run_private_suite(root, (selector,))
        message = str(caught.exception)
        self.assertNotIn("secret_selector", message)
        self.assertNotIn("tests/", message)

    def test_a_nonzero_exit_with_only_skips_is_not_refused(self) -> None:
        # A run that failed is the builder's to see, not the harness's to
        # reinterpret. Only an exit-0 suite that evaluated nothing refuses.
        with tempfile.TemporaryDirectory() as tree:
            with mock.patch.object(
                rr,
                "resolve",
                return_value=rr.ResolvedRunner(runner="pytest", executable="/bin/x"),
            ), mock.patch.object(
                rr,
                "execute_cases",
                return_value={"output": "2 skipped in 0.01s", "returncode": 2},
            ):
                out = tc.run_private_suite(Path(tree), ("tests/test_a.py",))
        self.assertEqual(out["returncode"], 2)
        self.assertEqual(out["executed"], 2)


def _capable_stub(path: Path, exit_code: int) -> Path:
    """A binary that passes `rr.probe` for its runner. Real, not mocked.

    `rr.resolve` runs the candidate and keys on its exit code, so a stub that
    exits with `CAPABLE_EXIT` is indistinguishable to it from a real runner.
    That is what lets these tests exercise the actual resolution ranking
    instead of asserting against a patched `rr.resolve`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit {0}\n".format(exit_code), encoding="utf-8")
    path.chmod(0o755)
    return path


class ResolutionRootIsTheExecutionTreeTest(unittest.TestCase):
    """pytest resolves where it EXECUTES; vitest resolves where its modules are.

    `rr.COLLECT_RUNTIME_DIRS` is `("node_modules",)`. vitest's environment is
    bridged from the runtime root into the tree, so resolving vitest against
    the runtime root is coherent. No Python environment is bridged, so a pytest
    resolved against the runtime root is the real repository's interpreter
    running against the candidate's source — missing deps at best, and at worst
    an editable install that imports the real repository's code and certifies
    it green.
    """

    @contextlib.contextmanager
    def _pair(self):
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as tree:
            yield Path(repo), Path(tree)

    def _run(self, tree: Path, repo: Path, files: tuple[str, ...], runner: str):
        summary = {
            "pytest": "1 passed in 0.01s",
            "vitest": "\n      Tests  1 passed (1)\n",
        }[runner]
        with mock.patch.object(
            rr, "execute_cases", return_value={"output": summary, "returncode": 0}
        ) as execute:
            out = tc.run_private_suite(tree, files, runtime_root=repo)
        return out, execute.call_args.args[0]

    def test_pytest_prefers_the_trees_environment_over_the_repositorys(self) -> None:
        with self._pair() as (repo, tree):
            _capable_stub(repo / ".venv" / "bin" / "pytest", 5)
            wanted = _capable_stub(tree / ".venv" / "bin" / "pytest", 5)
            _out, resolved = self._run(tree, repo, ("tests/test_a.py",), "pytest")
        self.assertEqual(resolved.runner, "pytest")
        self.assertEqual(Path(resolved.executable), wanted)

    def test_a_repository_venv_does_not_capture_pytest_resolution(self) -> None:
        with self._pair() as (repo, tree):
            trap = _capable_stub(repo / ".venv" / "bin" / "pytest", 5)
            _capable_stub(tree / ".venv" / "bin" / "pytest", 5)
            _out, resolved = self._run(tree, repo, ("tests/test_a.py",), "pytest")
            self.assertNotEqual(Path(resolved.executable), trap)
            self.assertNotIn(str(repo), resolved.executable)

    def test_pytest_refuses_when_the_tree_has_no_environment(self) -> None:
        # An explicit refusal beats silently running the repository's
        # interpreter against the candidate's source.
        with self._pair() as (repo, tree):
            _capable_stub(repo / ".venv" / "bin" / "pytest", 5)
            with mock.patch.object(
                rr, "resolve", side_effect=rr.RunnerUnusable
                ("pytest", rr.Reason.UNRESOLVED, ".")
            ):
                with self.assertRaises(pr.PrivateReviewError) as caught:
                    tc.run_private_suite(
                        tree, ("tests/test_a.py",), runtime_root=repo
                    )
        self.assertIn("SEALED_SUITE_RUNNER_UNUSABLE:pytest", str(caught.exception))

    def test_pytest_resolution_is_handed_the_tree_not_the_runtime_root(self) -> None:
        with self._pair() as (repo, tree):
            with mock.patch.object(
                rr,
                "resolve",
                return_value=rr.ResolvedRunner(runner="pytest", executable="/bin/x"),
            ) as resolve, mock.patch.object(
                rr,
                "execute_cases",
                return_value={"output": "1 passed in 0.01s", "returncode": 0},
            ):
                tc.run_private_suite(tree, ("tests/test_a.py",), runtime_root=repo)
            self.assertEqual(Path(resolve.call_args.args[1]), tree)

    def test_vitest_still_resolves_from_the_runtime_root(self) -> None:
        with self._pair() as (repo, tree):
            wanted = _capable_stub(repo / "node_modules" / ".bin" / "vitest", 0)
            _out, resolved = self._run(tree, repo, ("src/a.test.ts",), "vitest")
        self.assertEqual(resolved.runner, "vitest")
        self.assertEqual(Path(resolved.executable), wanted)

    def test_vitest_resolution_is_handed_the_runtime_root(self) -> None:
        with self._pair() as (repo, tree):
            with mock.patch.object(
                rr,
                "resolve",
                return_value=rr.ResolvedRunner(runner="vitest", executable="/bin/x"),
            ) as resolve, mock.patch.object(
                rr,
                "execute_cases",
                return_value={
                    "output": "\n      Tests  1 passed (1)\n",
                    "returncode": 0,
                },
            ):
                tc.run_private_suite(tree, ("src/a.test.ts",), runtime_root=repo)
            self.assertEqual(Path(resolve.call_args.args[1]), repo)

    def test_the_version_assertion_follows_the_resolved_pytest(self) -> None:
        with self._pair() as (repo, tree):
            _capable_stub(tree / ".venv" / "bin" / "pytest", 5)
            (tree / "tests").mkdir()
            (tree / "pyproject.toml").write_text(
                '[project]\nname = "candidate"\nrequires-python = ">=3.12"\n',
                encoding="utf-8",
            )
            if tc.tomllib is None:
                self.skipTest("tomllib is unavailable on this interpreter")
            with mock.patch.object(
                tc, "_interpreter_release", return_value=(3, 9, 6)
            ), mock.patch.object(rr, "execute_cases") as execute:
                with self.assertRaises(pr.PrivateReviewError) as caught:
                    tc.run_private_suite(
                        tree, ("tests/test_a.py",), runtime_root=repo
                    )
                execute.assert_not_called()
        message = str(caught.exception)
        self.assertIn("SEALED_SUITE_PYTHON_UNSUPPORTED", message)
        self.assertIn(str(tree / ".venv" / "bin" / "pytest"), message)


if __name__ == "__main__":
    unittest.main()
