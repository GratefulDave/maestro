"""`attempt salvage` derives the run's paths the configuration already holds (#83).

`run start` mints a run's directories as `repository_state / "runs" / <run_id>`
and hands the scheduler `worktrees/` and `scratch/` below it. Salvage takes the
run id positionally and reads the same `maestro.config.yaml`, so it holds both
halves of that derivation -- and used to mark both roots `required` anyway, at
the one moment an operator is least able to reconstruct a path by hand. The
failure was silent: a wrong `--worktrees-root` reports
`SALVAGE_WORKTREE_ABSENT`, which reads as "the work is gone" when the work is
fine and only the path was wrong.

These tests drive the real CLI entrypoint from inside a real configured
repository, against a real worktree and a real ledger, because the derivation
being tested is a property of the argument path rather than of the salvage
module -- calling `salvage_attempt` directly would prove nothing about it.

Run with:
    python3 -m pytest tests/test_salvage_path_defaults.py -o addopts= -q
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


DEAD_PID = 2_000_000_000
RUN_ID = "run-9f1c0a5d4c3b4e1e8a2f6b7c0d1e2f30"
NODE_ID = "lane-derived-paths"
FILE_A = "src/pkg/rules.py"
FILE_B = "tests/test_rules.py"
INVOKER = "operator@example"
REASON = "attempt died after writing both deliverables"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            "git " + " ".join(args) + " -> " + str(result.returncode)
            + ": " + result.stderr)
    return result.stdout.strip()


def _node() -> st.PlanNode:
    return st.PlanNode(
        node_id=NODE_ID, kind=st.NodeKind.CODE, depth=0,
        outputs=(FILE_A, FILE_B), command=("true",))


class _ConfiguredRepository:
    """An installed repository, a stranded attempt, and nothing typed twice.

    The layout is the configured default in every particular: the ledger sits
    at `repository_state / "lifecycle.sqlite3"` and the attempt's worktree at
    `repository_state / "runs" / <run_id> / "worktrees"`, exactly where
    `run start` would have put them. Nothing in the fixture tells the CLI where
    any of it is; that is the point of the fixture.
    """

    def __init__(self, *, worktrees_root: Path = None,
                 scratch_root: Path = None):
        self._relocated_worktrees = worktrees_root
        self._relocated_scratch = scratch_root

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = self._make_repo()
        self.state = (self.root / "maestro-state" / "repo").resolve()
        self._install_configuration()
        self.base = _git(self.repo, "rev-parse", "HEAD")

        run_root = self.state / "runs" / RUN_ID
        self.derived_worktrees = run_root / "worktrees"
        self.derived_scratch = run_root / "scratch"
        self.worktrees = self._relocated_worktrees or self.derived_worktrees
        self.scratch = self._relocated_scratch or self.derived_scratch
        self.records = self.root / "salvage-records"
        self.database = self.state / "lifecycle.sqlite3"

        self.store = lc.LifecycleStore(self.database)
        self.attempt = wt.create_attempt_worktree(
            repo=self.repo, run_id=RUN_ID, node_id=NODE_ID, attempt_no=1,
            integration_head=self.base, worktrees_root=self.worktrees,
            scratch_root=self.scratch)
        self.store.create_run(RUN_ID, "d" * 64, [_node()])
        self.store.start_attempt(RUN_ID, NODE_ID, base_sha=self.base)
        self.baseline = wt.take_baseline(self.attempt)
        self.store.record_baseline(
            RUN_ID, NODE_ID, 1, self.baseline,
            ignored_at_base=self.attempt.ignored_at_base)
        self._write_deliverables(self.attempt.path)
        self._strand()
        self.store.close()
        self.seed = rc.generate_seed()
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    # ── installation ─────────────────────────────────────────────────────────

    def _make_repo(self) -> Path:
        repo = self.root / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "harness@example.invalid")
        _git(repo, "config", "user.name", "Harness")
        _git(repo, "config", "core.hooksPath", str(self.root / "no-such-hooks"))
        (repo / "README.md").write_text("fixture\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "merge the previous lane")
        return repo

    def _install_configuration(self) -> None:
        plan_file = self.repo / "plans" / "named" / "maestro-plan.v1"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_bytes(b'{"plan":"stored bytes"}\n')
        (self.repo / "adws").mkdir()
        binaries = {}
        for name in ("herdr", "omp", "claude"):
            binary = self.root / name
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            binaries[name] = str(binary)
        route_dir = self.state / "route-receipts"
        route_dir.mkdir(parents=True)
        for route in ("omp", "claude"):
            (route_dir / (route + ".json")).write_text("{}", encoding="utf-8")
        seed = rc.generate_seed()
        route_seed = rc.generate_seed()
        self.environment = {
            "MAESTRO_TEST_VERIFY_KEY": rc.seed_to_public_key(seed).hex(),
            "MAESTRO_TEST_SIGNING_SEED": seed.hex(),
            "MAESTRO_TEST_ROUTE_VERIFY_KEY":
                rc.seed_to_public_key(route_seed).hex(),
        }
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
            "route_receipts": {"omp": "route-receipts/omp.json",
                               "claude": "route-receipts/claude.json"},
            "reviewer": {"route": "claude", "model": "review-model",
                         "effort": "high", "finalization_timeout_s": 60,
                         "turn_timeout_s": 20, "poll_interval_s": 1},
            "execution": {"route": "omp", "model": "execution-model",
                          "effort": "medium", "concurrency": 2,
                          "node_timeout_s": 120, "turn_timeout_s": 30,
                          "final_acceptance_timeout_s": 45,
                          "backstop_t_s": 600, "semantic_ceiling": 3},
        }
        (self.repo / "adws" / "maestro.config.yaml").write_text(
            json.dumps(config), encoding="utf-8")

    # ── the stranded attempt ─────────────────────────────────────────────────

    def _write_deliverables(self, path: Path) -> None:
        first = path / FILE_A
        second = path / FILE_B
        first.parent.mkdir(parents=True, exist_ok=True)
        second.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("RULES = True\n")
        second.write_text("def test_rules():\n    assert True\n")

    def _strand(self) -> None:
        self.store.declare_outcome(RUN_ID)
        self.store.conn.execute(
            "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
            (DEAD_PID, lc.scheduler_host(), RUN_ID))
        self.store.conn.execute(
            "UPDATE attempts SET launched_at=?, pid=?,"
            " attempt_host=?, attempt_start_epoch=?"
            " WHERE run_id=? AND node_id=? AND attempt_no=?",
            (1.0, DEAD_PID, lc.scheduler_host(), 1.0, RUN_ID, NODE_ID, 1))
        self.store.conn.commit()

    # ── invocation ───────────────────────────────────────────────────────────

    def salvage_argv(self, *extra: str):
        return [
            "attempt", "salvage", RUN_ID, NODE_ID, "1",
            "--invoked-by", INVOKER, "--reason", REASON,
            "--record-dir", str(self.records),
            "--signing-seed", self.seed.hex(), *extra,
        ]

    def cli(self, argv):
        """Invoke the CLI exactly as an operator does -- from the repository."""
        output = io.StringIO()
        previous = Path.cwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, self.environment, clear=False), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(argv)
        finally:
            os.chdir(previous)
        return code, output.getvalue()


class SalvageDerivesTheConfiguredLayout(unittest.TestCase):
    """The reported friction: four required paths, two of them derivable."""

    def test_a_configured_run_is_salvaged_with_no_path_flags(self):
        """`--worktrees-root`, `--scratch-root` and the ledger all derive."""
        with _ConfiguredRepository() as fixture:
            code, output = fixture.cli(fixture.salvage_argv())

            self.assertEqual(code, 0, output)
            payload = json.loads(output)
            self.assertEqual(payload["outcome"], "SALVAGED")
            self.assertEqual(payload["run_id"], RUN_ID)
            self.assertTrue(wt.is_attempt_output_commit(
                fixture.repo, payload["output_sha"], run_id=RUN_ID,
                node_id=NODE_ID, attempt_no=1, expected_base=fixture.base))
            listed = _git(
                fixture.repo, "diff-tree", "--no-commit-id", "--name-only",
                "-r", payload["output_sha"]).splitlines()
            self.assertEqual(sorted(listed), sorted([FILE_A, FILE_B]))

    def test_the_derived_roots_are_the_ones_run_start_mints(self):
        """Same derivation on both sides, spelled once (`maestro.py:1020`)."""
        args = maestro.build_parser().parse_args(
            ["attempt", "salvage", RUN_ID, NODE_ID, "1",
             "--invoked-by", INVOKER, "--reason", REASON,
             "--record-dir", "/tmp/records", "--signing-seed", "00"])
        with _ConfiguredRepository() as fixture:
            previous = Path.cwd()
            os.chdir(fixture.repo)
            try:
                maestro._bind_salvage_configuration(
                    args, tuple(fixture.salvage_argv()))
            finally:
                os.chdir(previous)

            self.assertEqual(Path(args.worktrees_root),
                             fixture.derived_worktrees)
            self.assertEqual(Path(args.scratch_root), fixture.derived_scratch)
            self.assertEqual(Path(args.db), fixture.database)


class AnExplicitFlagStillWins(unittest.TestCase):
    """A relocated or copied run directory is the case a default cannot answer."""

    def test_an_explicit_worktrees_root_overrides_the_derived_one(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            relocated = Path(elsewhere).resolve() / "relocated-worktrees"
            with _ConfiguredRepository(worktrees_root=relocated) as fixture:
                # The derived location holds nothing at all, so a salvage that
                # ignored the flag would refuse SALVAGE_WORKTREE_ABSENT.
                self.assertFalse(fixture.derived_worktrees.exists())

                code, output = fixture.cli(fixture.salvage_argv(
                    "--worktrees-root", str(relocated)))

                self.assertEqual(code, 0, output)
                payload = json.loads(output)
                self.assertEqual(payload["outcome"], "SALVAGED")
                self.assertTrue(wt.is_attempt_output_commit(
                    fixture.repo, payload["output_sha"], run_id=RUN_ID,
                    node_id=NODE_ID, attempt_no=1, expected_base=fixture.base))

    def test_an_explicit_scratch_root_overrides_the_derived_one(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            relocated = Path(elsewhere).resolve() / "relocated-scratch"
            with _ConfiguredRepository(scratch_root=relocated) as fixture:
                args = maestro.build_parser().parse_args(
                    fixture.salvage_argv("--scratch-root", str(relocated)))
                previous = Path.cwd()
                os.chdir(fixture.repo)
                try:
                    maestro._bind_salvage_configuration(
                        args, tuple(fixture.salvage_argv(
                            "--scratch-root", str(relocated))))
                finally:
                    os.chdir(previous)

                self.assertEqual(Path(args.scratch_root), relocated)
                self.assertEqual(Path(args.worktrees_root),
                                 fixture.derived_worktrees)

    def test_an_explicit_db_overrides_the_derived_one(self):
        with _ConfiguredRepository() as fixture:
            elsewhere = fixture.root / "elsewhere.sqlite3"
            args = maestro.build_parser().parse_args(
                fixture.salvage_argv("--db", str(elsewhere)))
            previous = Path.cwd()
            os.chdir(fixture.repo)
            try:
                maestro._bind_salvage_configuration(
                    args, tuple(fixture.salvage_argv("--db", str(elsewhere))))
            finally:
                os.chdir(previous)

            self.assertEqual(Path(args.db), elsewhere)


class SigningMaterialIsNeverDerived(unittest.TestCase):
    """A verb that quietly finds its own key is worse than one that asks."""

    def test_the_signing_seed_and_record_dir_stay_required(self):
        parser = maestro.build_parser()
        for omitted in ("--signing-seed", "--record-dir"):
            argv = ["attempt", "salvage", RUN_ID, NODE_ID, "1",
                    "--invoked-by", INVOKER, "--reason", REASON,
                    "--record-dir", "/tmp/records",
                    "--signing-seed", "00" * 32]
            index = argv.index(omitted)
            del argv[index:index + 2]
            with self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(argv)

    def test_no_configuration_refuses_by_flag_name_rather_than_crashing(self):
        """Outside an installed repository the roots are still the operator's."""
        with tempfile.TemporaryDirectory() as bare:
            previous = Path.cwd()
            os.chdir(bare)
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = maestro.main([
                        "attempt", "salvage", RUN_ID, NODE_ID, "1",
                        "--invoked-by", INVOKER, "--reason", REASON,
                        "--record-dir", str(Path(bare) / "records"),
                        "--signing-seed", "00" * 32])
            finally:
                os.chdir(previous)
            self.assertEqual(code, 3, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
            self.assertIn("--worktrees-root", payload["detail"])


if __name__ == "__main__":
    unittest.main()
