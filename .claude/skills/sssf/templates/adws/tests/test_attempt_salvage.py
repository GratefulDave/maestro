"""A stranded attempt's work is readmitted on that attempt's own ref.

`lane-p3-dedup-decisions` attempt 1 wrote both deliverables and died
before `commit_measured_delta`. The bytes sat in a1's worktree; the
factory had no verb that could admit them without attributing them to a2.

These tests drive the real verb against a real worktree and a real ledger.
Nothing is mocked on the measurement or the commit: those are the seams
the production incident sat on.

Run with:
    /Users/davidandrews/PycharmProjects/lexgenius-pipeline/.venv/bin/python -m pytest tests/test_attempt_salvage.py -o addopts= -q
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple


ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402
from adw_modules import salvage  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


DEAD_PID = 2_000_000_000
RUN_ID = "run-2a44d226e75a4be391a14f02b78a6d25"
NODE_ID = "lane-p3-dedup-decisions"
FILE_A = "src/lexgenius_pipeline/ingestion/judicial/cmo/dedup_rules.py"
FILE_B = "tests/unit/ingestion/test_cmo_dedup_rules.py"
INVOKER = "operator@example"
REASON = "attempt died after writing both deliverables"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} -> {result.returncode}: {result.stderr}")
    return result.stdout.strip()


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "Harness")
    _git(repo, "config", "core.hooksPath", str(root / "no-such-hooks"))
    (repo / "README.md").write_text("fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "merge lane-p2-manifest-import")
    return repo


def _node() -> st.PlanNode:
    return st.PlanNode(
        node_id=NODE_ID, kind=st.NodeKind.CODE, depth=0,
        outputs=(FILE_A, FILE_B), command=("true",))


def _strand(store: lc.LifecycleStore, base_sha: str, *,
            pid: int = DEAD_PID, launched: bool = True) -> None:
    store.create_run(RUN_ID, "d" * 64, [_node()])
    store.start_attempt(RUN_ID, NODE_ID, base_sha=base_sha)
    store.declare_outcome(RUN_ID)
    store.conn.execute(
        "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
        (pid, lc.scheduler_host(), RUN_ID))
    if launched:
        store.conn.execute(
            "UPDATE attempts SET launched_at=?, pid=? "
            "WHERE run_id=? AND node_id=? AND attempt_no=?",
            (1.0, pid, RUN_ID, NODE_ID, 1))


def _write_deliverables(path: Path) -> Tuple[str, str]:
    first = path / FILE_A
    second = path / FILE_B
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("RULES = True\n")
    second.write_text("def test_rules():\n    assert True\n")
    return (
        hashlib.sha256(first.read_bytes()).hexdigest(),
        hashlib.sha256(second.read_bytes()).hexdigest(),
    )


class _Harness:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = _make_repo(self.root)
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.worktrees = self.root / "worktrees"
        self.scratch = self.root / "scratch"
        self.records = self.root / "salvage"
        self.db = self.root / "lifecycle.db"
        self.store = lc.LifecycleStore(self.db)
        self.attempt = wt.create_attempt_worktree(
            repo=self.repo, run_id=RUN_ID, node_id=NODE_ID, attempt_no=1,
            integration_head=self.base, worktrees_root=self.worktrees,
            scratch_root=self.scratch)
        self.hashes = _write_deliverables(self.attempt.path)
        _strand(self.store, self.base)
        self.seed = rc.generate_seed()
        return self

    def __exit__(self, *exc):
        self.store.close()
        self._tmp.cleanup()

    def salvage(self, **overrides):
        kwargs = dict(
            store=self.store, run_id=RUN_ID, node_id=NODE_ID, attempt_no=1,
            repo=self.repo, worktrees_root=self.worktrees,
            scratch_root=self.scratch, invoked_by=INVOKER, reason=REASON,
            signing_seed=self.seed, record_dir=self.records)
        kwargs.update(overrides)
        return salvage.salvage_attempt(**kwargs)



class SalvageAdmitsTheStrandedAttempt(unittest.TestCase):
    """The accepted arm: a1's files become a1's output commit."""

    def test_a_stranded_attempt_is_committed_on_its_own_ref(self):
        """The production shape. Fails if salvage does not call
        commit_measured_delta on a1's own ref against a1's own base."""
        with _Harness() as h:
            result = h.salvage()

            self.assertTrue(wt.is_attempt_output_commit(
                h.repo, result.output_sha, run_id=RUN_ID, node_id=NODE_ID,
                attempt_no=1, expected_base=h.base))
            parent = _git(h.repo, "rev-parse", f"{result.output_sha}^")
            self.assertEqual(parent, h.base)
            listed = _git(
                h.repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                result.output_sha).splitlines()
            self.assertEqual(listed, [FILE_A, FILE_B])
            attempt = h.store.get_attempt(RUN_ID, NODE_ID, 1)
            self.assertEqual(attempt.extra["salvage_output_sha"], result.output_sha)
            self.assertIs(attempt.state, lc.CLOSED_ATTEMPT_STATE)
            self.assertIs(h.store.get_node(RUN_ID, NODE_ID).state,
                          st.NodeState.RUNNING)

    def test_the_signed_record_names_invoker_reason_and_file_digests(self):
        with _Harness() as h:
            result = h.salvage()
            payload = salvage.load_record(
                result.record_path, rc.seed_to_public_key(h.seed))
            self.assertEqual(payload["kind"], salvage.RECORD_KIND)
            self.assertEqual(payload["invoked_by"], INVOKER)
            self.assertEqual(payload["reason"], REASON)
            self.assertEqual(payload["output_sha"], result.output_sha)
            by_path = {row["path"]: row for row in payload["files"]}
            self.assertEqual(by_path[FILE_A]["sha256"], h.hashes[0])
            self.assertEqual(by_path[FILE_B]["sha256"], h.hashes[1])
            self.assertEqual(by_path[FILE_A]["action"], "added")


class SalvageRefuses(unittest.TestCase):
    """Every refusal the issue named, plus permission and live-attempt."""

    def test_an_undeclared_run_is_escape_refused(self):
        with _Harness() as h:
            h.store.conn.execute(
                "UPDATE runs SET latest_outcome=NULL, latest_outcome_at=NULL "
                "WHERE run_id=?", (RUN_ID,))
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()
            self.assertEqual(caught.exception.outcome, "ESCAPE_REFUSED")

    def test_a_missing_worktree_is_refused(self):
        with _Harness() as h:
            shutil.rmtree(h.attempt.path)

            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()
            self.assertEqual(caught.exception.outcome, "SALVAGE_WORKTREE_ABSENT")

    def test_a_moved_base_is_refused(self):
        with _Harness() as h:
            _git(h.attempt.path, "add", "-A")
            _git(h.attempt.path, "commit", "-qm", "agent committed on its own")
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()
            self.assertEqual(caught.exception.outcome, "SALVAGE_BASE_MOVED")

    def test_an_existing_output_commit_is_refused(self):
        with _Harness() as h:
            first = h.salvage()
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage(record_dir=h.root / "salvage-2")
            self.assertEqual(caught.exception.outcome, "SALVAGE_OUTPUT_EXISTS")
            self.assertEqual(
                _git(h.repo, "rev-parse",
                     f"refs/heads/{wt.branch_name(RUN_ID, NODE_ID, 1)}"),
                first.output_sha)

    def test_a_blank_invoker_is_refused(self):
        with _Harness() as h:
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage(invoked_by="  ")
            self.assertEqual(caught.exception.outcome, "SALVAGE_INVOKER_REQUIRED")

    def test_a_blank_reason_is_refused(self):
        with _Harness() as h:
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage(reason="")
            self.assertEqual(caught.exception.outcome, "SALVAGE_INVOKER_REQUIRED")

    def test_a_permission_failure_is_refused(self):
        with _Harness() as h:
            sneak = h.attempt.path / "elsewhere" / "stray.py"
            sneak.parent.mkdir(parents=True)
            sneak.write_text("outside the declaration\n")
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()
            self.assertEqual(caught.exception.outcome, "SALVAGE_PERMISSION_DENIED")
            self.assertIn("elsewhere/stray.py", caught.exception.fields["conjunct1"])
            self.assertEqual(
                _git(h.repo, "rev-parse",
                     f"refs/heads/{wt.branch_name(RUN_ID, NODE_ID, 1)}"),
                h.base)

    def test_a_live_attempt_is_refused(self):
        with _Harness() as h:
            h.store.conn.execute(
                "UPDATE attempts SET pid=? WHERE run_id=? AND node_id=? AND attempt_no=?",
                (os.getpid(), RUN_ID, NODE_ID, 1))
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()
            self.assertEqual(caught.exception.outcome, "SALVAGE_ATTEMPT_LIVE")

    def test_seeding_a_later_attempt_is_not_a_path(self):
        """a2 is a different worktree. Salvage names the attempt; it never
        copies a1's files into a2."""
        with _Harness() as h:
            h.store.retry(RUN_ID, NODE_ID)
            a2 = wt.create_attempt_worktree(
                repo=h.repo, run_id=RUN_ID, node_id=NODE_ID, attempt_no=2,
                integration_head=h.base, worktrees_root=h.worktrees,
                scratch_root=h.scratch)
            h.store.start_attempt(RUN_ID, NODE_ID, base_sha=h.base)

            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage(attempt_no=2)
            self.assertEqual(caught.exception.outcome, "SALVAGE_EMPTY_DELTA")
            self.assertFalse((a2.path / FILE_A).exists())


class SalvageCliTests(unittest.TestCase):

    def test_the_cli_handler_admits_the_reproduced_ledger(self):
        with _Harness() as h:
            h.store.close()
            args = SimpleNamespace(
                db=str(h.db), run_id=RUN_ID, node_id=NODE_ID, attempt_no=1,
                repo=str(h.repo), worktrees_root=str(h.worktrees),
                scratch_root=str(h.scratch), invoked_by=INVOKER, reason=REASON,
                signing_seed=h.seed.hex(), record_dir=str(h.records))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = maestro._attempt_salvage(args)
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["outcome"], "SALVAGED")
            self.assertTrue(wt.is_attempt_output_commit(
                h.repo, payload["output_sha"], run_id=RUN_ID, node_id=NODE_ID,
                attempt_no=1, expected_base=h.base))
            h.store = lc.LifecycleStore(h.db)

    def test_the_cli_prints_a_typed_refusal(self):
        with _Harness() as h:
            h.store.conn.execute(
                "UPDATE runs SET latest_outcome=NULL, latest_outcome_at=NULL "
                "WHERE run_id=?", (RUN_ID,))
            h.store.close()
            args = SimpleNamespace(
                db=str(h.db), run_id=RUN_ID, node_id=NODE_ID, attempt_no=1,
                repo=str(h.repo), worktrees_root=str(h.worktrees),
                scratch_root=str(h.scratch), invoked_by=INVOKER, reason=REASON,
                signing_seed=h.seed.hex(), record_dir=str(h.records))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = maestro._attempt_salvage(args)
            self.assertEqual(code, 3)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["outcome"], "ESCAPE_REFUSED")
            h.store = lc.LifecycleStore(h.db)


if __name__ == "__main__":
    unittest.main()
