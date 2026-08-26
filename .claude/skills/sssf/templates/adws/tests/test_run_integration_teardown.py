"""A run releases the integration checkout it created, and only that one."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sqlite3
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
        capture_output=True,
        text=True,
        check=False,
    )


def _worktree_paths(repo):
    """Resolved, because git reports the real path behind /var -> /private/var."""
    listed = _git(repo, "worktree", "list", "--porcelain")
    return {
        Path(line[len("worktree ") :].strip()).resolve()
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ")
    }


class _RunSeamHarness:
    """A git repository, a plan, and the seam `_execute_run` is driven through.

    Shared by the teardown tests and the reclaim tests below, because both ask
    what a run does to worktrees and a second copy of this fixture would let
    the two answers drift apart.
    """

    def _repository(self, root):
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "maestro@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Maestro Test"], cwd=repo, check=True
        )
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
        subprocess.run(["git", "branch", BRANCH], cwd=repo, check=True)
        return repo

    def _arguments(self, root, repo, integration, repository_state=None):
        # `argparse.Namespace` rather than `SimpleNamespace`: it takes the same
        # arbitrary keywords, and it is what `_run_start` and `_execute_run`
        # actually declare, so the seam is checked instead of merely duck-typed.
        arguments = argparse.Namespace(
            plan_file=str(root / "plan.json"),
            db=str(root / "state.db"),
            run_id="run-1",
            integration_path=str(integration),
            repo=str(repo),
            data_dir=str(root / "data"),
            receipt_dir=str(root / "receipts"),
            worktrees_root=str(root / "worktrees"),
            scratch_root=str(root / "scratch"),
            digest="a" * 64,
        )
        # Bound only by installed repository configuration, so a hand-spelled
        # run genuinely arrives without it -- and must, or the reclaim would be
        # tested against a boundary the real unconfigured path never has.
        if repository_state is not None:
            arguments.repository_state = str(repository_state)
        return arguments

    def _plan(self):
        return SimpleNamespace(
            agent_nodes=(),
            merge_policy=SimpleNamespace(
                integration_branch=BRANCH,
                integration_gate=SimpleNamespace(runner="none", argv=(), min_cases=1),
            ),
            to_plan_nodes=lambda: (),
        )

    @contextlib.contextmanager
    def _run_seam(self, scheduler_class):
        gate = SimpleNamespace()
        with (
            mock.patch.object(maestro, "_run_configuration", return_value=mock.Mock()),
            mock.patch.object(
                maestro, "_load_runnable_plan", return_value=self._plan()
            ),
            mock.patch.object(maestro, "_validate_run_paths"),
            mock.patch.object(
                maestro, "_scheduler_gate_deps", return_value=(gate, gate)
            ),
            mock.patch.object(
                # Runner resolution probes a real interpreter, which is what
                # these tests are not about. It joins the four seams above for
                # the same reason they are here: this file asks what a run does
                # to worktrees, and every other question is stubbed.
                maestro,
                "_resolve_run_runners",
                return_value={},
            ),
            mock.patch.object(maestro.lc, "LifecycleStore"),
            mock.patch.object(maestro.scheduler, "Scheduler", scheduler_class),
        ):
            yield

    @staticmethod
    def _accepting_scheduler(side_effect=None):
        class AcceptingScheduler:
            def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                self.deps = deps

            def project(self):
                return None

            def run(self):
                if side_effect is not None:
                    side_effect(Path(self.deps.integration_path))
                return SimpleNamespace(
                    outcome=scheduler_types.RunOutcome.ACCEPTED, merged=(), blocked=()
                )

        return AcceptingScheduler


class RunIntegrationTeardownTest(_RunSeamHarness, unittest.TestCase):
    """§8.8 keeps the branch; nothing keeps the run's own checkout of it."""

    def test_accepted_run_releases_its_checkout_and_keeps_the_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            output = io.StringIO()
            with (
                self._run_seam(self._accepting_scheduler()),
                contextlib.redirect_stdout(output),
            ):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["outcome"], "ACCEPTED")
            self.assertFalse(integration.exists())
            self.assertNotIn(integration.resolve(), _worktree_paths(repo))
            self.assertEqual(
                _git(repo, "rev-parse", "--verify", BRANCH).returncode,
                0,
                "the integration branch is the only copy of merged work",
            )

    def test_gate_artifacts_do_not_strand_the_checkout(self):
        """The integration gate runs in there; its litter is not evidence."""

        def litter(integration):
            (integration / "untracked.log").write_text("gate\n", encoding="utf-8")
            (integration / "README.md").write_text("modified\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            with (
                self._run_seam(self._accepting_scheduler(litter)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 0)
            self.assertFalse(integration.exists())
            self.assertEqual(_git(repo, "rev-parse", "--verify", BRANCH).returncode, 0)

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
                with (
                    self._run_seam(self._accepting_scheduler()),
                    contextlib.redirect_stdout(output),
                ):
                    code = maestro._run_start(self._arguments(root, repo, integration))
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
                cwd=repo,
                check=True,
            )
            (occupant / "operator-work.txt").write_text("mine\n", encoding="utf-8")
            integration = root / "integration"
            output = io.StringIO()
            with (
                self._run_seam(self._accepting_scheduler()),
                contextlib.redirect_stdout(output),
            ):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 3)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["outcome"], "INTEGRATION_BRANCH_CHECKED_OUT")
            self.assertTrue(occupant.is_dir())
            self.assertEqual(
                (occupant / "operator-work.txt").read_text(encoding="utf-8"), "mine\n"
            )
            self.assertIn(occupant.resolve(), _worktree_paths(repo))
            self.assertFalse(integration.exists())

    def test_release_neither_swallows_nor_masks_a_failing_run(self):
        class ExplodingScheduler:
            def __init__(self, _run_id, _nodes, _config, _deps, **_kwargs):
                pass

            def project(self):
                return None

            def run(self):
                raise RuntimeError("scheduler exploded")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            with self._run_seam(ExplodingScheduler):
                with self.assertRaises(RuntimeError) as raised:
                    maestro._execute_run(
                        self._arguments(root, repo, integration), resuming=False
                    )

            self.assertEqual(str(raised.exception), "scheduler exploded")
            self.assertFalse(integration.exists())
            self.assertNotIn(integration.resolve(), _worktree_paths(repo))
            self.assertEqual(_git(repo, "rev-parse", "--verify", BRANCH).returncode, 0)

    def test_a_checkout_this_run_did_not_create_is_left_where_it_is(self):
        """Resume reuses an existing integration worktree; it does not own it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repository(root)
            integration = root / "integration"
            subprocess.run(
                ["git", "worktree", "add", "-q", str(integration), BRANCH],
                cwd=repo,
                check=True,
            )
            with (
                self._run_seam(self._accepting_scheduler()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = maestro._run_start(self._arguments(root, repo, integration))

            self.assertEqual(code, 0)
            self.assertTrue(integration.is_dir())
            self.assertIn(integration.resolve(), _worktree_paths(repo))


class RunStartReclaimsItsOwnLeftoverTest(_RunSeamHarness, unittest.TestCase):
    """`run start` clears its own litter, and refuses over everyone else's.

    Two questions, and a reclaim needs both answered. *Whose* checkout is this
    is path containment against the configured run root, never a name or a
    claim (§1.2): a checkout under `<repository state>/runs` was created by
    this system, and one anywhere else may be the operator's. *Is the run that
    owns it still resumable* is that run's own recorded outcome, read from the
    ledger. Containment alone once decided this, and a run that was merely
    blocked lost its merges to a verb that never asked (§19 M24).

    The harness stubs `lc.LifecycleStore`, so nothing here creates the ledger
    a real run always has by the time the reclaim runs -- `_execute_run`
    constructs the store above the block the reclaim sits in. `_record_run`
    writes the two columns the predicate reads, so these tests state the run's
    state instead of inheriting "no ledger" from the stub.
    """

    def _installation(self, root):
        repo = self._repository(root)
        state = root / "state"
        (state / "runs").mkdir(parents=True)
        return repo, state

    def _record_run(self, root, outcome, run_id="run-0"):
        """The ledger row a real run has written by the time it holds a branch.

        `latest_outcome` NULL is the crashed-run shape and is spelled by
        passing `None`; the column exists either way, because a row is
        inserted when the run starts and only the declaration is missing.
        """
        with sqlite3.connect(root / "state.db") as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY,"
                " latest_outcome TEXT)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO runs (run_id, latest_outcome) VALUES (?, ?)",
                (run_id, outcome),
            )

    def _strand_integration(self, repo, state, run_id="run-0"):
        """What a run that died before its release leaves holding the branch."""
        stranded = state / "runs" / run_id / "integration"
        stranded.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-q", str(stranded), BRANCH],
            cwd=repo,
            check=True,
        )
        return stranded

    def _start(self, root, repo, state, run_id="run-1"):
        integration = state / "runs" / run_id / "integration"
        integration.parent.mkdir(parents=True, exist_ok=True)
        output = io.StringIO()
        with (
            self._run_seam(self._accepting_scheduler()),
            contextlib.redirect_stdout(output),
        ):
            code = maestro._run_start(
                self._arguments(root, repo, integration, repository_state=state)
            )
        return code, json.loads(output.getvalue()), integration

    def test_a_stranded_checkout_in_our_own_run_root_is_reclaimed(self):
        """The verb's documented subject: a release that failed and said so."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, state = self._installation(root)
            stranded = self._strand_integration(repo, state)
            self._record_run(root, "ACCEPTED")

            code, payload, _ = self._start(root, repo, state)

            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["outcome"], "ACCEPTED")
            self.assertFalse(
                stranded.exists(),
                "a previous run's own integration checkout is this system's "
                "litter, not an operator's work",
            )
            self.assertNotIn(stranded.resolve(), _worktree_paths(repo))
            self.assertEqual(
                _git(repo, "rev-parse", "--verify", BRANCH).returncode,
                0,
                "reclaiming a checkout never takes the branch with it",
            )

    def test_an_operator_checkout_outside_the_run_root_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, state = self._installation(root)
            occupant = root / "operator"
            subprocess.run(
                ["git", "worktree", "add", "-q", str(occupant), BRANCH],
                cwd=repo,
                check=True,
            )
            (occupant / "operator-work.txt").write_text("mine\n", encoding="utf-8")

            code, payload, integration = self._start(root, repo, state)

            self.assertEqual(code, 3, payload)
            self.assertEqual(payload["outcome"], "INTEGRATION_BRANCH_CHECKED_OUT")
            self.assertTrue(occupant.is_dir())
            self.assertEqual(
                (occupant / "operator-work.txt").read_text(encoding="utf-8"), "mine\n"
            )
            self.assertIn(occupant.resolve(), _worktree_paths(repo))
            self.assertFalse(integration.exists())

    def test_retained_blocked_attempt_worktrees_are_never_swept(self):
        """§8.8 keeps a blocked node's worktree for post-mortem. Keep it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, state = self._installation(root)
            stranded = self._strand_integration(repo, state)
            self._record_run(root, "ACCEPTED")
            retained = []
            for node in ("node-a", "node-b"):
                attempt = state / "runs" / "run-0" / "worktrees" / node
                subprocess.run(
                    [
                        "git",
                        "worktree",
                        "add",
                        "-q",
                        "-b",
                        "attempt/" + node + "-1",
                        str(attempt),
                        "main",
                    ],
                    cwd=repo,
                    check=True,
                )
                (attempt / "evidence.txt").write_text(node + "\n", encoding="utf-8")
                retained.append(attempt)

            code, payload, _ = self._start(root, repo, state)

            self.assertEqual(code, 0, payload)
            self.assertFalse(stranded.exists())
            listed = _worktree_paths(repo)
            for attempt in retained:
                self.assertTrue(
                    attempt.is_dir(),
                    "a blocked node's worktree is post-mortem evidence",
                )
                self.assertEqual(
                    (attempt / "evidence.txt").read_text(encoding="utf-8"),
                    attempt.name + "\n",
                )
                self.assertIn(attempt.resolve(), listed)
                self.assertEqual(
                    _git(
                        repo, "rev-parse", "--verify", "attempt/" + attempt.name + "-1"
                    ).returncode,
                    0,
                )

    def test_a_blocked_runs_checkout_is_refused_rather_than_reclaimed(self):
        """A run an operator can resume is not this system's litter (§19 M24).

        `run start` reclaims *without asking*, which is right for a leftover
        and wrong for a run holding merges. The refusal names the run and the
        state that produced it, so the operator can act on it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, state = self._installation(root)
            stranded = self._strand_integration(repo, state)
            self._record_run(root, "BLOCKED")
            (stranded / "merged-work.txt").write_text("kept\n", encoding="utf-8")

            code, payload, integration = self._start(root, repo, state)

            self.assertEqual(code, 3, payload)
            self.assertEqual(payload["outcome"], "INTEGRATION_WORKTREE_RUN_NOT_OVER")
            self.assertIn("run-0", payload["detail"])
            self.assertIn("BLOCKED", payload["detail"])
            self.assertTrue(stranded.is_dir())
            self.assertEqual(
                (stranded / "merged-work.txt").read_text(encoding="utf-8"), "kept\n"
            )
            self.assertIn(stranded.resolve(), _worktree_paths(repo))
            self.assertFalse(integration.exists())

    def test_a_crashed_runs_checkout_is_refused_rather_than_reclaimed(self):
        """A NULL outcome is a run nothing ever declared quiescence for.

        This is the behaviour change worth naming: before the state check,
        `run start` silently reclaimed a crashed run's integration checkout
        and carried on. That checkout may hold merged work, and destroying it
        costs a `DurableOutputIdentityError` on the next resume rather than an
        error at the moment of destruction, so it is now the operator's call.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, state = self._installation(root)
            stranded = self._strand_integration(repo, state)
            self._record_run(root, None)

            code, payload, _ = self._start(root, repo, state)

            self.assertEqual(code, 3, payload)
            self.assertEqual(payload["outcome"], "INTEGRATION_WORKTREE_RUN_NOT_OVER")
            self.assertIn("run-0", payload["detail"])
            self.assertIn("no declared outcome", payload["detail"])
            self.assertTrue(stranded.is_dir())
            self.assertIn(stranded.resolve(), _worktree_paths(repo))

    def test_the_operator_override_releases_a_crashed_runs_checkout(self):
        """The route out of the refusal above, and the only one (§11.3).

        `run start` has no flag of its own -- the operator resumes the run, or
        ends it for good. `deliver`'s `--discard-live-runs` reaches the same
        shared predicate, so this asserts the escape at the function both
        callers cross rather than at one verb's argument parser.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, state = self._installation(root)
            stranded = self._strand_integration(repo, state)
            self._record_run(root, None)

            released = maestro._reclaim_stranded_integration_worktree(
                repo, state / "runs", BRANCH, root / "state.db", discard_live=True
            )

            # Resolved, because the path comes back through `git worktree
            # list`, which reports the real path behind /var -> /private/var.
            self.assertEqual(released, stranded.resolve())
            self.assertFalse(stranded.exists())
            self.assertNotIn(stranded.resolve(), _worktree_paths(repo))
            self.assertEqual(
                _git(repo, "rev-parse", "--verify", BRANCH).returncode,
                0,
                "discarding a checkout never takes the branch with it",
            )


if __name__ == "__main__":
    unittest.main()
