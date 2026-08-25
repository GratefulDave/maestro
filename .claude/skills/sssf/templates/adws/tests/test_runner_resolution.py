"""The binary that adjudicates a node is decided, probed, and recorded.

Recorded failure. `Gate.runner` is a literal, and three places turned that
literal into `argv[0]` and executed it with an inherited `PATH`. Measured on
the machine this was written on:

    which pytest                        -> /opt/homebrew/bin/pytest (8.4.0)
    pytest --collect-only …             -> ModuleNotFoundError: structlog
    .venv/bin/pytest --collect-only …   -> 10 tests collected

Same worktree, same argv. The consequence is not that the gate goes red, it is
that it goes red *unparseably*: no summary line, empty counts,
`GateCounts.parse` returns `None`, `adjudicate_gate` stamps `ENVIRONMENTAL`,
and the node re-runs an identically broken interpreter until its environmental
budget is gone. An operator is told a budget ran out when nothing was ever
retryable.

The three properties these tests hold:

* capability is an **exit code from a probe**, never stderr text and never the
  gate's own exit — measured, a broken interpreter on an existing test file
  and a good interpreter on a missing one both exit 4;
* a **declared** runner that fails its probe refuses and does not fall back to
  discovery, because a declaration that silently guesses is not a declaration;
* the refusal is a **run precondition**, so it launches nothing, writes no
  attempt row, and never reaches the retry classifier — which is what keeps it
  distinguishable from a lane that legitimately collected under its floor.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import maestro                                       # noqa: E402
from adw_modules import plan_validate as pv          # noqa: E402
from adw_modules import runner_resolution as rr      # noqa: E402
from adw_modules import scheduler_types as st        # noqa: E402
from adw_modules import verification                 # noqa: E402
from adw_modules import worktree as wt               # noqa: E402


def _script(path: Path, body: str) -> Path:
    """A fake runner: a shell script whose exit code is the whole contract."""
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TheProbeIsMeasuredAgainstARealInterpreterTest(unittest.TestCase):
    """The one empirical fact this design rests on, as a standing assertion.

    Every other test here fakes the runner with a script whose exit code is
    hardcoded, which proves the plumbing and nothing about the measurement.
    `CAPABLE_EXIT["pytest"] == 5` compared against itself is a constant
    compared against itself.

    The failure that leaves open is concrete. Change `PROBE_ARGS["pytest"]`'s
    `-k` value to a pattern that *matches* real tests and a capable runner
    exits 0 rather than 5, so every run on every machine refuses INCAPABLE —
    and the rest of this file stays green, because it asserts that `-k` is
    present and never that its pattern selects nothing. These two tests run
    the real interpreter and are the only thing between a one-token edit to
    that table and a fleet-wide refusal.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self._tmp.name)
        # A tree with real collectable tests, so "collected nothing" is a
        # statement about the selector rather than about an empty directory.
        (self.tree / "test_probe_subject.py").write_text(
            "def test_one():\n    assert True\n\n"
            "def test_two():\n    assert True\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_real_pytest_returns_the_capable_exit_and_collects_nothing(self):
        """`5` is `pytest.ExitCode.NO_TESTS_COLLECTED`, and this asserts the
        runner actually returns it for this argv rather than that the constant
        equals itself."""
        code = rr.probe("pytest", (sys.executable, "-m", "pytest"), self.tree)
        self.assertEqual(code, rr.CAPABLE_EXIT["pytest"])
        self.assertEqual(
            code,
            __import__("pytest").ExitCode.NO_TESTS_COLLECTED.value,
            "the capable exit is pytest's own published enum member")

    def test_the_probe_selector_is_what_makes_the_tree_collect_nothing(self):
        """The control for the test above. The same interpreter, the same
        tree, the same flags — with the no-match selector removed, collection
        succeeds and the exit is no longer the capable one. Without this, a
        `-k` pattern that matched everything would still look measured."""
        args = list(rr.PROBE_ARGS["pytest"])
        index = args.index("-k")
        del args[index:index + 2]
        result = subprocess.run(
            [sys.executable, "-m", "pytest"] + args, cwd=str(self.tree),
            capture_output=True, text=True, timeout=rr.PROBE_TIMEOUT_S)
        self.assertNotEqual(result.returncode, rr.CAPABLE_EXIT["pytest"])
        self.assertIn("2 tests collected", result.stdout)

    def test_an_interpreter_without_pytest_does_not_look_capable(self):
        """The negative half. A runner that cannot start must not return the
        capable exit, or `resolve` would accept an interpreter that runs
        nothing."""
        missing = self.tree / "no-such-interpreter"
        code = rr.probe("pytest", (str(missing),), self.tree)
        self.assertNotEqual(code, rr.CAPABLE_EXIT["pytest"])
        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.tree, declared=str(missing))
        self.assertIn(caught.exception.reason,
                      (rr.Reason.UNRESOLVED, rr.Reason.INCAPABLE))

    def test_the_real_interpreter_resolves_through_the_public_entry(self):
        """`resolve` end to end against a real interpreter, so the declared
        path, the probe, and the recorded version are one measurement rather
        than three stubs."""
        wrapper = self.tree / "pytest-wrapper"
        _script(wrapper, 'exec "{0}" -m pytest "$@"'.format(sys.executable))
        resolved = rr.resolve("pytest", self.tree, declared=str(wrapper))
        self.assertEqual(resolved.probe_exit, rr.CAPABLE_EXIT["pytest"])
        self.assertTrue(resolved.version.strip(), "the version line is recorded")


class ProbeTest(unittest.TestCase):
    """The probe is the only thing that decides capability."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.bin = self.repo / "bin"
        self.bin.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_capable_exit_is_no_tests_collected(self):
        """`5` is `pytest.ExitCode.NO_TESTS_COLLECTED`, public stable API, and
        it is the only exit the probe accepts."""
        self.assertEqual(rr.CAPABLE_EXIT["pytest"], 5)

    def test_the_probe_selects_nothing_and_clears_addopts(self):
        """Decoupled from the plan's selectors on purpose, and `-o addopts=`
        is mandatory: a repository setting `-W error` there would turn a
        capable probe into MAX_WARNINGS_ERROR."""
        args = rr.PROBE_ARGS["pytest"]
        self.assertIn("--collect-only", args)
        self.assertIn("-k", args)
        self.assertEqual(args[args.index("-o") + 1], "addopts=")
        self.assertEqual(args[args.index("-p") + 1], "no:cacheprovider")

    def test_a_capable_runner_resolves_and_carries_its_probe_exit(self):
        runner = _script(self.bin / "pytest", "exit 5")
        resolved = rr.resolve("pytest", self.repo,
                              declared=str(runner))
        self.assertEqual(resolved.executable, str(runner))
        self.assertEqual(resolved.origin, "declared")
        self.assertEqual(resolved.probe_exit, 5)
        self.assertEqual(resolved.execute_argv(("-q", "t.py")),
                         (str(runner), "-q", "t.py"))

    def test_a_declared_runner_that_cannot_collect_refuses_as_incapable(self):
        """Exit 4 is what the homebrew pytest returns against a conftest it
        cannot import. It resolves, it starts, and it is refused."""
        runner = _script(self.bin / "pytest", "exit 4")
        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.repo, declared=str(runner))
        self.assertIs(caught.exception.reason, rr.Reason.INCAPABLE)
        self.assertEqual(caught.exception.probe_exit, 4)
        self.assertEqual(caught.exception.resolved, str(runner))

    def test_a_declared_runner_never_falls_back_to_discovery(self):
        """A capable candidate sits at discovery rank 1 the whole time. The
        declaration still refuses, because a declaration that quietly picks
        something else is not a declaration."""
        (self.repo / ".venv" / "bin").mkdir(parents=True)
        _script(self.repo / ".venv" / "bin" / "pytest", "exit 5")
        declared = _script(self.bin / "pytest", "exit 4")
        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.repo, declared=str(declared))
        self.assertIs(caught.exception.reason, rr.Reason.INCAPABLE)
        self.assertEqual(caught.exception.resolved, str(declared))

    def test_a_declared_value_that_is_not_executable_is_unresolved(self):
        (self.repo / "not-a-runner").write_text("", encoding="utf-8")
        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.repo, declared="not-a-runner")
        self.assertIs(caught.exception.reason, rr.Reason.UNRESOLVED)

    def test_a_declared_relative_path_anchors_at_the_repository(self):
        (self.repo / ".venv" / "bin").mkdir(parents=True)
        runner = _script(self.repo / ".venv" / "bin" / "pytest", "exit 5")
        resolved = rr.resolve("pytest", self.repo,
                              declared=".venv/bin/pytest")
        self.assertEqual(Path(resolved.executable).resolve(), runner.resolve())

    def test_discovery_prefers_the_repository_environment_and_says_so(self):
        (self.repo / ".venv" / "bin").mkdir(parents=True)
        runner = _script(self.repo / ".venv" / "bin" / "pytest", "exit 5")
        resolved = rr.resolve("pytest", self.repo)
        self.assertEqual(Path(resolved.executable).resolve(), runner.resolve())
        self.assertEqual(resolved.origin, "discovered")
        notice = rr.adoption_notice([resolved])
        self.assertIn("runners:", notice)
        self.assertIn(str(runner), notice)

    def test_two_candidates_at_one_rank_are_ambiguous_and_both_are_named(self):
        for directory in (".venv", "venv"):
            (self.repo / directory / "bin").mkdir(parents=True)
            _script(self.repo / directory / "bin" / "pytest", "exit 5")
        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.repo)
        self.assertIs(caught.exception.reason, rr.Reason.AMBIGUOUS)
        self.assertEqual(len(caught.exception.candidates), 2)
        self.assertIn(".venv", " ".join(caught.exception.candidates))
        self.assertIn("/venv", " ".join(caught.exception.candidates))

    def test_vitest_uses_its_measured_zero_case_probe(self):
        (self.repo / "node_modules" / ".bin").mkdir(parents=True)
        runner = _script(
            self.repo / "node_modules" / ".bin" / "vitest", "exit 0")

        resolved = rr.resolve("vitest", self.repo)

        self.assertEqual(Path(resolved.executable).resolve(), runner.resolve())
        self.assertEqual(resolved.probe_exit, 0)

    def test_the_refusal_payload_carries_every_discriminating_fact(self):
        """§3.6 B15: a field with no reader is a build failure. Each of these
        is read here, and `reason` is the field a caller branches on rather
        than parsing the sentence."""
        runner = _script(self.bin / "pytest", "exit 4")
        with self.assertRaises(rr.RunnerUnusable) as caught:
            rr.resolve("pytest", self.repo, declared=str(runner))
        payload = caught.exception.payload()
        self.assertEqual(payload["outcome"], "RUNNER_UNUSABLE")
        self.assertEqual(payload["reason"], "INCAPABLE")
        self.assertEqual(payload["runner"], "pytest")
        self.assertEqual(payload["probe_exit"], 4)
        self.assertEqual(payload["resolved"], str(runner))
        self.assertEqual(payload["candidates"], [str(runner)])
        self.assertEqual(payload["cwd"], ".")
        self.assertNotIn("detail", payload)


class RecordTest(unittest.TestCase):
    def test_the_resolution_is_written_beside_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "runs" / "run-1" / "runner-resolution.json"
            resolved = rr.ResolvedRunner(
                runner="pytest", executable="/abs/.venv/bin/pytest",
                origin="discovered", probe_exit=5, version="pytest 9.1.1")
            rr.write_record(destination, [resolved])
            rows = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["executable"], "/abs/.venv/bin/pytest")
        self.assertEqual(rows[0]["origin"], "discovered")
        self.assertEqual(rows[0]["probe_exit"], 5)
        self.assertEqual(rows[0]["version"], "pytest 9.1.1")


class ExecutorAcceptsOnlyAResolvedRunnerTest(unittest.TestCase):
    """The chokepoint is a signature, not a convention.

    §19 M6 records the cost of the alternative: the handoff size check sat on
    one launch path while another route skipped it. A new call site here has
    nothing to build an invocation from.
    """

    def test_a_bare_command_is_refused_by_the_gate_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TypeError):
                wt.run_integration_gate(
                    Path(tmp), ("pytest", "-q"), (), Path(tmp) / "scratch",
                    lambda: False)

    def test_the_collector_builds_its_argv_from_a_resolved_runner(self):
        source = (ADWS / "adw_modules" / "plan_validate.py").read_text()
        self.assertNotIn('"pytest", "--collect-only"', source)
        self.assertIn("collect_argv", source)


class DistinguishableFromAnUnderFloorCollectionTest(unittest.TestCase):
    """Three conditions, three disjoint paths.

    A selector that legitimately collects fewer cases than its floor is a
    parseable failing report and stays `SEMANTIC`. An unusable runner never
    produces a verdict at all. A runner that breaks mid-run still parses as
    unparseable and stays `ENVIRONMENTAL`, because a venv deleted at minute
    forty really is an environmental fault.
    """

    def gate_result(self, exit_code, counts):
        return wt.GateResult(
            label="node-gate", scope="node", selector="tests/test_a.py",
            command=("/abs/.venv/bin/pytest", "tests/test_a.py"),
            exit_code=exit_code, green=exit_code == 0, counts=counts)

    def test_an_under_floor_collection_is_semantic(self):
        verdict = verification.adjudicate_gate(
            self.gate_result(1, {"passed": 1, "failed": 1}), min_cases=1)
        self.assertFalse(verdict.green)
        self.assertFalse(verdict.unparseable)
        self.assertIsNone(verdict.retry_class)

    def test_an_unparseable_report_is_still_environmental(self):
        verdict = verification.adjudicate_gate(
            self.gate_result(4, {}), min_cases=1)
        self.assertTrue(verdict.unparseable)
        self.assertIs(verdict.retry_class, st.RetryClass.ENVIRONMENTAL)

    def test_an_unusable_runner_produces_no_verdict_at_all(self):
        """It is a run precondition, so there is nothing to adjudicate: no
        attempt row, no gate result, no retry class. The refusal carries the
        typed reason instead."""
        failure = rr.RunnerUnusable("pytest", rr.Reason.INCAPABLE, ".",
                                    resolved="/usr/bin/pytest", probe_exit=4)
        self.assertNotIn("retry_class", failure.payload())
        self.assertEqual(failure.payload()["outcome"], "RUNNER_UNUSABLE")
        self.assertNotIn(
            failure.payload()["reason"],
            {member.value for member in st.RetryClass})


class RunStartRefusesBeforeAnythingIsRecordedTest(unittest.TestCase):
    """The refusal is a run precondition, not an attempt outcome.

    That is the whole reason it is not a fourth `RetryClass` and not a new
    `BlockReason`: §7.5 closes the retry set at three, there is nothing to
    retry, and a block reason describes a node that ran and stopped. Under
    this design no node ever starts — so no store is opened, no attempt row is
    written, and no classifier is reached.
    """

    def _plan(self, runner="pytest"):
        return SimpleNamespace(
            agent_nodes=(),
            merge_policy=SimpleNamespace(
                integration_branch="integration",
                integration_gate=SimpleNamespace(
                    runner=runner, argv=(), min_cases=1)),
            to_plan_nodes=lambda: ())

    def _arguments(self, root):
        return SimpleNamespace(
            plan_file=str(root / "plan.json"), db=str(root / "state.db"),
            run_id="run-1", integration_path=str(root / "integration"),
            repo=str(root), data_dir=str(root / "data"),
            receipt_dir=str(root / "receipts"),
            worktrees_root=str(root / "worktrees"),
            scratch_root=str(root / "scratch"), digest="a" * 64)

    def test_an_unusable_runner_refuses_with_the_typed_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            output = io.StringIO()
            store = mock.Mock()
            with mock.patch.object(maestro, "_run_configuration",
                                   return_value=mock.Mock()), \
                    mock.patch.object(maestro, "_load_runnable_plan",
                                      return_value=self._plan("vitest")), \
                    mock.patch.object(maestro, "_validate_run_paths"), \
                    mock.patch.object(maestro.lc, "LifecycleStore", store), \
                    contextlib.redirect_stdout(output):
                code = maestro._execute_run(self._arguments(root),
                                            resuming=False)
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUNNER_UNUSABLE")
        self.assertEqual(payload["reason"], "UNRESOLVED")
        self.assertEqual(payload["runner"], "vitest")
        self.assertIn("detail", payload)
        # No ledger was opened, so there is no attempt row to classify.
        store.assert_not_called()

    def test_a_usable_runner_is_recorded_beside_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / ".venv" / "bin").mkdir(parents=True)
            _script(root / ".venv" / "bin" / "pytest", "exit 5")
            arguments = self._arguments(root)
            with mock.patch.object(maestro, "_run_configuration",
                                   return_value=mock.Mock()), \
                    mock.patch.object(maestro, "_load_runnable_plan",
                                      return_value=self._plan()), \
                    mock.patch.object(maestro, "_validate_run_paths"):
                resolved = maestro._resolve_run_runners(
                    arguments, self._plan())
            self.assertEqual(resolved["pytest"].origin, "discovered")
            self.assertEqual(resolved["pytest"].probe_exit, 5)


class DeclaredRunnerConfigTest(unittest.TestCase):
    """`runners:` is optional, so an existing configuration keeps loading."""

    def _installation(self, root):
        repo = root / "project"
        (repo / "adws").mkdir(parents=True)
        (repo / "plans").mkdir(parents=True)
        (repo / ".git").mkdir()
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = root / name
            _script(binary, "exit 0")
            binaries[name] = str(binary)
        return repo, {
            "schema": "maestro-config.v1",
            "plans_dir": "plans",
            "state_root": "../maestro-state",
            "keys": {"verify_key_env": "V", "signing_seed_env": "S",
                     "route_verify_key_env": "R"},
            "executables": binaries,
            "route_receipts": {"omp": "route-receipts/omp.json"},
            "reviewer": {"route": "omp", "model": "m", "effort": "high",
                         "finalization_timeout_s": 60, "turn_timeout_s": 20,
                         "poll_interval_s": 1},
            "execution": {"route": "omp", "model": "m", "effort": "medium",
                          "concurrency": 2, "node_timeout_s": 120,
                          "turn_timeout_s": 30,
                          "final_acceptance_timeout_s": 45,
                          "backstop_t_s": 600, "semantic_ceiling": 3},
        }

    def _layout(self, config, repo):
        path = repo / "adws" / "maestro.config.yaml"
        path.write_text(json.dumps(config), encoding="utf-8")
        return maestro._load_maestro_layout(repo, path)

    def test_a_configuration_without_the_block_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, config = self._installation(Path(tmp).resolve())
            self.assertEqual(self._layout(config, repo)["runners"], {})

    def test_a_declared_runner_reaches_the_layout_unresolved(self):
        """Carried as authored, not passed through `shutil.which`: a runner is
        legitimately spelled as a repository-relative path, which is not on
        PATH, and its usability is decided by the capability probe."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, config = self._installation(Path(tmp).resolve())
            config["runners"] = {"pytest": ".venv/bin/pytest"}
            self.assertEqual(self._layout(config, repo)["runners"],
                             {"pytest": ".venv/bin/pytest"})

    def test_an_unknown_runner_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, config = self._installation(Path(tmp).resolve())
            config["runners"] = {"jest": "node_modules/.bin/jest"}
            with self.assertRaises(maestro._MaestroConfigurationError):
                self._layout(config, repo)



class IntegrationGateStaysUnscopedTest(unittest.TestCase):
    """G1 — final acceptance runs the plan's unscoped command.

    A named subset that covers every lane still misses a test no lane owned.
    Concatenating the union of lane specs is the same hole. The adapter
    strips selectors and ignores `specs`.
    """

    def test_named_files_in_the_plan_argv_are_not_the_executed_argv(self):
        named = (
            "-q",
            "tests/unit/ingestion/test_cmo_gap_policy.py",
            "tests/unit/ingestion/test_cmo_table_isolation.py",
        )
        plan = SimpleNamespace(
            merge_policy=SimpleNamespace(
                integration_gate=SimpleNamespace(
                    runner="pytest", argv=named)))
        runner = rr.ResolvedRunner(
            runner="pytest", executable="/abs/.venv/bin/pytest",
            origin="declared", probe_exit=5, version="stub")
        captured = {}

        def fake_run(worktree_path, resolved, argv, scratch, cancel_requested,
                     label="integration-gate"):
            captured["argv"] = tuple(argv)
            captured["label"] = label
            return None

        lane_union = (
            "tests/unit/ingestion/test_cmo_gap_policy.py",
            "tests/lane_only.py",
        )
        with mock.patch.object(
                maestro.worktree, "run_integration_gate", side_effect=fake_run):
            _, run_ig = maestro._scheduler_gate_deps(plan, {"pytest": runner})
            run_ig(Path("/tmp/integration"), lane_union, lambda: False)

        self.assertEqual(captured["argv"], ("-q",))
        self.assertNotIn(
            "tests/unit/ingestion/test_cmo_table_isolation.py",
            captured["argv"])
        self.assertNotIn("tests/lane_only.py", captured["argv"])
        self.assertNotIn("-o", captured["argv"])
        self.assertEqual(captured["label"], "integration-gate")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
