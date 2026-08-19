"""A stranded RUNNING node is reachable by every escape once the scheduler is dead.

The live shape (run-2a44d226e75a4be391a14f02b78a6d25, 2026-08-19):

* `runs.latest_outcome` = BLOCKED
* `runs.cancel_requested` = 0
* `runs.scheduler_pid` points at a process that is gone
* one node still RUNNING, attempt 1 still open

`retry` refused that node because it gated on NODE state (`expected state in
('BLOCKED',)`). The race that gate exists to prevent is a RUN-level fact:
whether a scheduler process is still there. These tests drive the real verbs
against a fixture ledger built the same way.

Run with:
    python -m pytest tests/test_stranded_running_escape.py -o addopts= -q
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
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402


DEAD_PID = 2_000_000_000
RUN_ID = "run-2a44d226e75a4be391a14f02b78a6d25"
NODE_ID = "lane-p3-dedup-decisions"


def make_node(node_id: str, depth: int = 0, needs=()) -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=depth,
                       needs=tuple(needs), command=("true",))


def _stamp_scheduler(store: lc.LifecycleStore, run_id: str, *,
                     pid, host=None) -> None:
    store.conn.execute(
        "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
        (pid, host if host is not None else lc.scheduler_host(), run_id))

def _stranded_blocked(store: lc.LifecycleStore, *,
                      pid=DEAD_PID, host=None,
                      stuck: bool = False) -> None:
    """The reproduced shape: declared BLOCKED/STUCK, node still RUNNING."""
    store.create_run(RUN_ID, "d" * 64, [make_node(NODE_ID)])
    store.start_attempt(RUN_ID, NODE_ID, base_sha="a" * 40)
    store.declare_outcome(RUN_ID, stuck=stuck)
    _stamp_scheduler(store, RUN_ID, pid=pid, host=host)


def _init_git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                   check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"],
                   check=True)
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


class StrandedRunningRetryTests(unittest.TestCase):
    """The exact live state, and the two refusals that must stay fail-closed."""

    def test_retry_admits_running_when_run_is_blocked_and_scheduler_is_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store)
            self.assertEqual(store.latest_outcome(RUN_ID), st.RunOutcome.BLOCKED)
            self.assertIs(store.get_node(RUN_ID, NODE_ID).state, st.NodeState.RUNNING)
            self.assertFalse(wd.process_is_alive(DEAD_PID))


            row = store.retry(RUN_ID, NODE_ID)

            self.assertIs(row.state, st.NodeState.PENDING)
            self.assertEqual(row.attempt_no, 1)
            self.assertEqual(store.ready_nodes(RUN_ID), (NODE_ID,))
            attempt = store.get_attempt(RUN_ID, NODE_ID, 1)
            self.assertIs(attempt.state, lc.CLOSED_ATTEMPT_STATE)
            self.assertIs(attempt.retry_class, st.RetryClass.ENVIRONMENTAL)
            store.start_attempt(RUN_ID, NODE_ID, base_sha="b" * 40)
            self.assertEqual(store.get_node(RUN_ID, NODE_ID).attempt_no, 2)
            store.close()

    def test_retry_admits_the_same_shape_when_the_declaration_is_stuck(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store, stuck=True)
            self.assertEqual(store.latest_outcome(RUN_ID), st.RunOutcome.STUCK)
            row = store.retry(RUN_ID, NODE_ID)
            self.assertIs(row.state, st.NodeState.PENDING)
            store.close()

    def test_retry_refuses_when_the_scheduler_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store, pid=os.getpid())
            with self.assertRaises(lc.SchedulerStillAlive) as caught:
                store.retry(RUN_ID, NODE_ID)
            self.assertIs(caught.exception.refusal,
                          st.EscapeRefusal.SCHEDULER_STILL_ALIVE)
            self.assertIs(store.get_node(RUN_ID, NODE_ID).state,
                          st.NodeState.RUNNING)
            store.close()

    def test_retry_refuses_when_liveness_is_unknown_because_host_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store, pid=DEAD_PID, host="other-machine")
            with self.assertRaises(lc.SchedulerLivenessUnknown) as caught:
                store.retry(RUN_ID, NODE_ID)
            self.assertIs(caught.exception.refusal,
                          st.EscapeRefusal.SCHEDULER_LIVENESS_UNKNOWN)
            self.assertIn("not this host", str(caught.exception))
            self.assertIs(store.get_node(RUN_ID, NODE_ID).state,
                          st.NodeState.RUNNING)
            store.close()

    def test_retry_refuses_when_liveness_is_unknown_because_pid_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store, pid=None, host=None)
            with self.assertRaises(lc.SchedulerLivenessUnknown) as caught:
                store.retry(RUN_ID, NODE_ID)
            self.assertIn("no scheduler pid is recorded", str(caught.exception))
            store.close()

    def test_maestro_retry_verb_admits_the_reproduced_ledger(self):
        """Drive the CLI handler, not just the store method."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            _stranded_blocked(store)
            store.close()
            args = SimpleNamespace(
                db=str(db), command="retry", run_id=RUN_ID,
                node_id=NODE_ID, force=False)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = maestro._escape(args)
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["node_id"], NODE_ID)
            self.assertEqual(payload["state"], st.NodeState.PENDING.value)
            store = lc.LifecycleStore(db)
            self.assertIs(store.get_node(RUN_ID, NODE_ID).state,
                          st.NodeState.PENDING)
            store.close()


class StrandedRunningSkipTests(unittest.TestCase):

    def test_skip_admits_running_when_scheduler_is_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(NODE_ID)])
            store.start_attempt(RUN_ID, NODE_ID, base_sha=sha)
            store.declare_outcome(RUN_ID)
            _stamp_scheduler(store, RUN_ID, pid=DEAD_PID)
            row = store.skip(RUN_ID, NODE_ID, accept_sha=sha, repo_path=repo)
            self.assertIs(row.state, st.NodeState.MERGED)
            self.assertEqual(row.output_sha, sha)
            attempt = store.get_attempt(RUN_ID, NODE_ID, 1)
            self.assertIs(attempt.state, lc.CLOSED_ATTEMPT_STATE)
            self.assertIsNone(attempt.retry_class)
            store.close()

    def test_skip_refuses_when_the_scheduler_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(NODE_ID)])
            store.start_attempt(RUN_ID, NODE_ID, base_sha=sha)
            store.declare_outcome(RUN_ID)
            _stamp_scheduler(store, RUN_ID, pid=os.getpid())
            with self.assertRaises(lc.SchedulerStillAlive):
                store.skip(RUN_ID, NODE_ID, accept_sha=sha, repo_path=repo)
            self.assertIs(store.get_node(RUN_ID, NODE_ID).state,
                          st.NodeState.RUNNING)
            store.close()

    def test_skip_refuses_when_liveness_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(NODE_ID)])
            store.start_attempt(RUN_ID, NODE_ID, base_sha=sha)
            store.declare_outcome(RUN_ID)
            _stamp_scheduler(store, RUN_ID, pid=DEAD_PID, host="other-machine")
            with self.assertRaises(lc.SchedulerLivenessUnknown):
                store.skip(RUN_ID, NODE_ID, accept_sha=sha, repo_path=repo)
            store.close()


class StrandedRunningAbandonTests(unittest.TestCase):

    def test_abandon_admits_running_when_scheduler_is_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store)
            row = store.abandon(RUN_ID, NODE_ID)
            self.assertIs(row.state, st.NodeState.CANCELLED)
            attempt = store.get_attempt(RUN_ID, NODE_ID, 1)
            self.assertIs(attempt.state, lc.CLOSED_ATTEMPT_STATE)
            store.close()

    def test_abandon_refuses_when_the_scheduler_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store, pid=os.getpid())
            with self.assertRaises(lc.SchedulerStillAlive):
                store.abandon(RUN_ID, NODE_ID)
            self.assertIs(store.get_node(RUN_ID, NODE_ID).state,
                          st.NodeState.RUNNING)
            store.close()

    def test_abandon_refuses_when_liveness_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            _stranded_blocked(store, pid=None, host=None)
            with self.assertRaises(lc.SchedulerLivenessUnknown):
                store.abandon(RUN_ID, NODE_ID)
            store.close()


class EscapeRefusalVocabularyTests(unittest.TestCase):

    def test_refusal_names_are_the_exception_vocabulary(self):
        self.assertEqual(
            {item.value for item in st.EscapeRefusal},
            {"SCHEDULER_STILL_ALIVE", "SCHEDULER_LIVENESS_UNKNOWN"})
        self.assertIs(lc.SchedulerStillAlive.refusal,
                      st.EscapeRefusal.SCHEDULER_STILL_ALIVE)
        self.assertIs(lc.SchedulerLivenessUnknown.refusal,
                      st.EscapeRefusal.SCHEDULER_LIVENESS_UNKNOWN)
