"""A run releases the integration checkout it created, and only that one."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import scheduler_types


BRANCH = "integration/run-1"


def _git(repo, *arguments):
    return subprocess.run(
        ("git", "-C", str(repo)) + arguments,
        capture_output=True, text=True, check=False)


def _worktree_paths(repo):
    """Resolved, because git reports the real path behind /var -> /private/var."""
    listed = _git(repo, "worktree", "list", "--porcelain")
    return {
        Path(line[len("worktree "):].strip()).resolve()
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    }


class RunIntegrationTeardownTest(unittest.TestCase):
    """§8.8 keeps the branch; nothing keeps the run's own checkout of it."""

    def _repository(self, root):
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "maestro@example.invalid"],
            cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Maestro Test"], cwd=repo, check=True)
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
        subprocess.run(["git", "branch", BRANCH], cwd=repo, check=True)
        return repo

    def _arguments(self, root, repo, integration):
        return SimpleNamespace(
            plan_file=str(root / "plan.json"), db=str(root / "state.db"),
            run_id="run-1", integration_path=str(integration), repo=str(repo),
            data_dir=str(root / "data"), receipt_dir=str(root / "receipts"),
            worktrees_root=str(root / "worktrees"),
            scratch_root=str(root / "scratch"), digest="a" * 64)

    def _plan(self):
        return SimpleNamespace(
            agent_nodes=(),
            merge_policy=SimpleNamespace(
                integration_branch=BRANCH,
                integration_gate=SimpleNamespace(runner="none", argv=())),
            to_plan_nodes=lambda: ())

    @contextlib.contextmanager
    def _run_seam(self, scheduler_class):
        gate = SimpleNamespace()
        with mock.patch.object(
                maestro, "_run_configuration", return_value=mock.Mock()
        ), mock.patch.object(
                maestro, "_load_runnable_plan", return_value=self._plan()
        ), mock.patch.object(
                maestro, "_validate_run_paths"
        ), mock.patch.object(
                maestro, "_scheduler_gate_deps", return_value=(gate, gate)
        ), mock.patch.object(
                maestro.lc, "LifecycleStore"
        ), mock.patch.object(
                maestro.scheduler, "Scheduler", scheduler_class):
            yield

    @staticmethod
    def _accepting_scheduler(side_effect=None):
        class AcceptingScheduler:
            def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                self.deps = deps

            def run(self):
                if side_effect is not None:
                    side_effect(Path(self.deps.integration_path))
                return SimpleNamespace(
                    outcome=scheduler_types.RunOutcome.ACCEPTED,
                    merged=(), blocked=())

        return AcceptingScheduler

    def test_accepted_run_releases_its_checkout_and_keeps_the_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            output = io.StringIO()
            with self._run_seam(self._accepting_scheduler()), \
                    contextlib.redirect_stdout(output):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(
                json.loads(output.getvalue())["outcome"], "ACCEPTED")
            self.assertFalse(integration.exists())
            self.assertNotIn(integration.resolve(), _worktree_paths(repo))
            self.assertEqual(
                _git(repo, "rev-parse", "--verify", BRANCH).returncode, 0,
                "the integration branch is the only copy of merged work")

    def test_gate_artifacts_do_not_strand_the_checkout(self):
        """The integration gate runs in there; its litter is not evidence."""
        def litter(integration):
            (integration / "untracked.log").write_text("gate\n", encoding="utf-8")
            (integration / "README.md").write_text("modified\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            with self._run_seam(self._accepting_scheduler(litter)), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 0)
            self.assertFalse(integration.exists())
            self.assertEqual(
                _git(repo, "rev-parse", "--verify", BRANCH).returncode, 0)

    def test_second_run_of_the_same_plan_is_not_refused(self):
        """Each run gets its own `<run_root>/integration`, so run two would
        otherwise meet run one's abandoned checkout holding the branch."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            outcomes = []
            for run in ("run-1", "run-2"):
                integration = root / "runs" / run / "integration"
                integration.parent.mkdir(parents=True)
                output = io.StringIO()
                with self._run_seam(self._accepting_scheduler()), \
                        contextlib.redirect_stdout(output):
                    code = maestro._run_start(
                        self._arguments(root, repo, integration))
                outcomes.append((code, json.loads(output.getvalue())["outcome"]))

            self.assertEqual(outcomes, [(0, "ACCEPTED"), (0, "ACCEPTED")])

    def test_refusal_leaves_the_occupant_worktree_alone(self):
        """The occupant may be the operator's own checkout. Never touch it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            occupant = root / "occupant"
            subprocess.run(
                ["git", "worktree", "add", "-q", str(occupant), BRANCH],
                cwd=repo, check=True)
            (occupant / "operator-work.txt").write_text("mine\n", encoding="utf-8")
            integration = root / "integration"
            output = io.StringIO()
            with self._run_seam(self._accepting_scheduler()), \
                    contextlib.redirect_stdout(output):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 3)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["outcome"], "INTEGRATION_BRANCH_CHECKED_OUT")
            self.assertTrue(occupant.is_dir())
            self.assertEqual(
                (occupant / "operator-work.txt").read_text(encoding="utf-8"),
                "mine\n")
            self.assertIn(occupant.resolve(), _worktree_paths(repo))
            self.assertFalse(integration.exists())

    def test_release_neither_swallows_nor_masks_a_failing_run(self):
        class ExplodingScheduler:
            def __init__(self, _run_id, _nodes, _config, _deps, **_kwargs):
                pass

            def run(self):
                raise RuntimeError("scheduler exploded")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            with self._run_seam(ExplodingScheduler):
                with self.assertRaises(RuntimeError) as raised:
                    maestro._execute_run(
                        self._arguments(root, repo, integration), resuming=False)

            self.assertEqual(str(raised.exception), "scheduler exploded")
            self.assertFalse(integration.exists())
            self.assertNotIn(integration.resolve(), _worktree_paths(repo))
            self.assertEqual(
                _git(repo, "rev-parse", "--verify", BRANCH).returncode, 0)

    def test_a_checkout_this_run_did_not_create_is_left_where_it_is(self):
        """Resume reuses an existing integration worktree; it does not own it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            subprocess.run(
                ["git", "worktree", "add", "-q", str(integration), BRANCH],
                cwd=repo, check=True)
            with self._run_seam(self._accepting_scheduler()), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 0)
            self.assertTrue(integration.is_dir())
            self.assertIn(integration.resolve(), _worktree_paths(repo))


if __name__ == "__main__":
    unittest.main()
