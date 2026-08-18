"""D10 — §8.3's provision step runs, in the attempt's own worktree.

`LauncherAdapter.provision` (§9.3's sixth operation) has been implemented since
it was written and `Scheduler._attempt` has called `deps.provision` since the
scheduler existed. Nothing had ever supplied either end: `HerdrLauncher` was
constructed without `provision_argv`, so its `provision` returned on its first
line, and `SchedulerDeps.provision` was left at `None`, so the scheduler's call
was skipped.

The consequence is invisible under pytest, whose provision step is §9.3's
no-op, and total in any repository with an install step. §8.3's baseline is
measured against an unprovisioned tree, and §7.4's falsifiable gate stops
meaning anything: a pre-node gate red because `node_modules` is absent is not
red for the intended reason, and its post-node partner cannot go green, so the
node blocks on a fact about the checkout rather than about the work.

Every test here drives the production path. Asserting that `HerdrLauncher`
provisions when handed an argv would have stayed green throughout the defect,
because that half always worked; what had no writer was the argv and the
scheduler dependency.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import launcher
from adw_modules import scheduler_types


def _marker_argv(marker: Path, *, exit_code: int = 0) -> list:
    """A provision command that records the directory it ran in."""
    return [
        sys.executable, "-c",
        "import os,sys;open(sys.argv[1],'a').write(os.getcwd()+chr(10));"
        "sys.stderr.write('provision said no');sys.exit(int(sys.argv[2]))",
        str(marker), str(exit_code),
    ]


class ConfigurationCarriesTheProvisionArgvTest(unittest.TestCase):
    """Fact 1 — `execution.provision` survives the load and reaches `args`."""

    def _installation(self, root: Path, provision) -> Path:
        repo = root / "project"
        (repo / "adws").mkdir(parents=True)
        (repo / "plans").mkdir(parents=True)
        (repo / ".git").mkdir()
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = root / name
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        execution = {
            "route": "omp", "model": "execution-model", "effort": "medium",
            "concurrency": 2, "node_timeout_s": 120, "turn_timeout_s": 30,
            "final_acceptance_timeout_s": 45, "backstop_t_s": 600,
            "semantic_ceiling": 3,
        }
        if provision is not None:
            execution["provision"] = provision
        config = {
            "schema": "maestro-config.v1",
            "plans_dir": "plans",
            "state_root": "../maestro-state",
            "keys": {
                "verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
                "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
                "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY",
            },
            "executables": binaries,
            "route_receipts": {"omp": "route-receipts/omp.json"},
            "reviewer": {
                "route": "omp", "model": "review-model", "effort": "high",
                "finalization_timeout_s": 60, "turn_timeout_s": 20,
                "poll_interval_s": 1,
            },
            "execution": execution,
        }
        path = repo / "adws" / "maestro.config.yaml"
        path.write_text(json.dumps(config), encoding="utf-8")
        return repo, path

    def test_a_configured_provision_command_is_loaded_as_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo, path = self._installation(root, ["npm", "ci"])
            layout = maestro._load_maestro_layout(repo, path)
        self.assertEqual(layout["execution"]["provision"], ("npm", "ci"))

    def test_an_absent_provision_command_is_the_no_op_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo, path = self._installation(root, None)
            layout = maestro._load_maestro_layout(repo, path)
        self.assertEqual(layout["execution"]["provision"], ())

    def test_a_shell_string_is_refused_rather_than_split(self):
        """`docs/plan-authoring.md`'s rule for gates, applied here: whatever
        split a string would be a shell this process never runs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo, path = self._installation(root, "npm ci")
            with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                maestro._load_maestro_layout(repo, path)
        self.assertIn("execution.provision", str(caught.exception))

    def test_an_empty_provision_list_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo, path = self._installation(root, [])
            with self.assertRaises(maestro._MaestroConfigurationError):
                maestro._load_maestro_layout(repo, path)


class TheLauncherIsBuiltWithTheProvisionArgvTest(unittest.TestCase):
    """Fact 2 — `_runtime_launcher` hands the adapter the argv to run.

    Without this the adapter's `provision` returns on its first line for every
    attempt of every run, which is what it did.
    """

    def _args(self, provision_argv):
        return SimpleNamespace(
            herdr="/bin/sh", omp="/bin/sh", claude="/bin/sh",
            agent_route="omp", agent_model="m", agent_effort="high",
            route_receipt=["omp=/dev/null"], route_verify_key=["ab"],
            provision_argv=provision_argv)

    def _build(self, provision_argv):
        with mock.patch.object(
                maestro.route_receipts, "load_admitted_routes",
                return_value=mock.Mock(spec=launcher.AdmittedRouteSet)):
            return maestro._runtime_launcher(self._args(provision_argv))

    def test_the_configured_argv_reaches_the_adapter(self):
        runtime = self._build(["npm", "ci"])
        self.assertEqual(runtime.provision_argv, ("npm", "ci"))

    def test_no_configured_argv_leaves_the_adapter_a_no_op(self):
        runtime = self._build(None)
        self.assertEqual(runtime.provision_argv, ())


class TheProvisionerRunsInTheAttemptWorktreeTest(unittest.TestCase):
    """Fact 3 — what `_run_provisioner` returns actually provisions.

    §9.3 puts provision on the runner adapter, and a plan of code nodes alone
    has no runner adapter, so both arms are asserted here and both are asserted
    to refuse identically. A second implementation that drifted from the
    launcher's would be the two-representations shape all over again.
    """

    def _provision(self, argv, *, agent_nodes):
        args = SimpleNamespace(provision_argv=list(argv))
        route_runner = (
            launcher.HerdrLauncher(
                herdr_path=Path("/bin/sh"), omp_path=Path("/bin/sh"),
                claude_path=Path("/bin/sh"),
                admitted_routes=mock.Mock(spec=launcher.AdmittedRouteSet),
                provision_argv=tuple(argv))
            if agent_nodes else None)
        return maestro._run_provisioner(args, route_runner)

    def test_provision_runs_with_the_worktree_as_its_working_directory(self):
        for agent_nodes in (True, False):
            with self.subTest(agent_nodes=agent_nodes), \
                    tempfile.TemporaryDirectory() as tmp:
                worktree = Path(tmp).resolve()
                marker = worktree / "ran.txt"
                provision = self._provision(
                    _marker_argv(marker), agent_nodes=agent_nodes)
                provision(worktree)
                self.assertEqual(
                    marker.read_text(encoding="utf-8").strip(), str(worktree))

    def test_a_failing_provision_refuses_identically_on_both_arms(self):
        messages = {}
        for agent_nodes in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                worktree = Path(tmp).resolve()
                provision = self._provision(
                    _marker_argv(worktree / "ran.txt", exit_code=3),
                    agent_nodes=agent_nodes)
                with self.assertRaises(RuntimeError) as caught:
                    provision(worktree)
                messages[agent_nodes] = str(caught.exception)
        self.assertTrue(messages[True].startswith("PROVISION_FAILED:"),
                        messages[True])
        self.assertEqual(messages[True], messages[False],
                         "the code-only provisioner has drifted from the "
                         "adapter's: " + repr(messages))

    def test_nothing_configured_supplies_no_provisioner(self):
        """§9.3's stated default. A pytest repository wants exactly this."""
        self.assertIsNone(self._provision((), agent_nodes=False))


class TheSchedulerIsGivenTheProvisionStepTest(unittest.TestCase):
    """Fact 4 — `maestro run start` puts it on `SchedulerDeps`.

    This is the one the audit named. Facts 1 to 3 were all reachable while this
    was `None`, and while it was `None` none of them ran.
    """

    def _drive(self, plan, *, provision_argv, route_runner):
        captured = {}

        class CapturingScheduler:
            def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                captured["deps"] = deps

            def run(self):
                return SimpleNamespace(
                    outcome=SimpleNamespace(value="ACCEPTED"),
                    merged=(), blocked=(), review_findings={})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            (root / "integration").mkdir(parents=True)
            args = SimpleNamespace(
                plan_file=str(root / "plan.json"),
                db=str(Path(tmp) / "state.db"), run_id="run-1",
                integration_path=str(root / "integration"), repo=str(root),
                data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
                worktrees_root=str(root / "worktrees"),
                scratch_root=str(root / "scratch"), digest="a" * 64,
                agent_route="omp", agent_model="model", agent_effort="high",
                agent_profile="profile", provision_argv=provision_argv)
            output = io.StringIO()
            with mock.patch.object(
                    maestro, "_run_configuration", return_value=mock.Mock()
            ), mock.patch.object(
                    maestro, "_load_runnable_plan", return_value=plan
            ), mock.patch.object(
                    maestro, "_runtime_launcher", return_value=route_runner
            ), mock.patch.object(
                    maestro.lc, "LifecycleStore"
            ), mock.patch.object(
                    maestro.scheduler, "Scheduler", CapturingScheduler
            ), contextlib.redirect_stdout(output):
                code = maestro._run_start(args)
        self.assertEqual(code, 0, output.getvalue())
        return captured["deps"]

    @staticmethod
    def _plan(*, agent_nodes):
        node = SimpleNamespace(
            kind=scheduler_types.NodeKind.AGENT, node_id="agent")
        return SimpleNamespace(
            agent_nodes=(node,) if agent_nodes else (),
            merge_policy=SimpleNamespace(
                integration_branch="main",
                integration_gate=SimpleNamespace(
                    runner="none", argv=(), min_cases=1)),
            node_by_id=lambda: {
                "agent": SimpleNamespace(instruction="do the work")},
            to_plan_nodes=lambda: ())

    def test_an_agent_run_gives_the_scheduler_the_adapters_provision(self):
        route_runner = launcher.HerdrLauncher(
            herdr_path=Path("/bin/sh"), omp_path=Path("/bin/sh"),
            claude_path=Path("/bin/sh"),
            admitted_routes=mock.Mock(spec=launcher.AdmittedRouteSet),
            provision_argv=("npm", "ci"))
        deps = self._drive(self._plan(agent_nodes=True),
                           provision_argv=["npm", "ci"],
                           route_runner=route_runner)
        self.assertIsNotNone(
            deps.provision,
            "SchedulerDeps.provision is None, so §8.3's provision step is "
            "skipped and the baseline is measured against an unprovisioned "
            "tree")
        self.assertEqual(deps.provision, route_runner.provision)

    def test_a_code_only_run_still_provisions(self):
        deps = self._drive(self._plan(agent_nodes=False),
                           provision_argv=["npm", "ci"], route_runner=None)
        self.assertIsNotNone(
            deps.provision,
            "a plan of code nodes alone has no runner adapter, and its "
            "worktree still needs provisioning before §8.3's baseline")

    def test_a_run_with_nothing_configured_provisions_nothing(self):
        deps = self._drive(self._plan(agent_nodes=False),
                           provision_argv=None, route_runner=None)
        self.assertIsNone(deps.provision)


if __name__ == "__main__":
    unittest.main()
