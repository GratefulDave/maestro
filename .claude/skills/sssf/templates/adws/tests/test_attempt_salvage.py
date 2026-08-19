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


#: A declared output that is glob-shaped, which is the ordinary case: §8.3
#: says declared outputs are name-shaped and matched as globs.
CMO_GLOB = "src/lexgenius_pipeline/ingestion/judicial/cmo/*.py"
PROVISIONED = "src/lexgenius_pipeline/ingestion/judicial/cmo/__init__.py"
PROVISIONED_BYTES = "# provisioned by the adapter, not by any attempt\n"


def _globbed_node() -> st.PlanNode:
    """The same node, declaring its outputs as a glob rather than by name."""
    return st.PlanNode(
        node_id=NODE_ID, kind=st.NodeKind.CODE, depth=0,
        outputs=(CMO_GLOB, FILE_B), command=("true",))


def _provision_untracked(path: Path) -> None:
    """What an adapter's `provision` leaves behind: untracked, not ignored,
    and under a directory the node declares."""
    target = path / PROVISIONED
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(PROVISIONED_BYTES)


def _node() -> st.PlanNode:
    return st.PlanNode(
        node_id=NODE_ID, kind=st.NodeKind.CODE, depth=0,
        outputs=(FILE_A, FILE_B), command=("true",))


def _open_attempt(store: lc.LifecycleStore, base_sha: str, *,
                  node: st.PlanNode = None) -> None:
    """The ledger writes that precede the baseline, in the scheduler's order."""
    store.create_run(RUN_ID, "d" * 64, [node or _node()])
    store.start_attempt(RUN_ID, NODE_ID, base_sha=base_sha)


def _strand(store: lc.LifecycleStore, *,
            pid: int = DEAD_PID, launched: bool = True) -> None:
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
    """The scheduler's attempt order, reproduced: worktree, attempt row,
    provision, baseline (taken *and recorded*), then the agent's writes.

    `provision` is what makes the harness able to state the defect at all. The
    baseline is the provisioned tree, and a provisioned path exists in no
    commit, so nothing downstream can rebuild it from the base SHA.
    """

    def __init__(self, node: st.PlanNode = None, provision=None,
                 record_baseline: bool = True):
        self._node = node or _node()
        self._provision = provision
        self._record_baseline = record_baseline

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
        _open_attempt(self.store, self.base, node=self._node)
        if self._provision is not None:
            self._provision(self.attempt.path)
        self.baseline = wt.take_baseline(self.attempt)
        if self._record_baseline:
            self.baseline_digest = self.store.record_baseline(
                RUN_ID, NODE_ID, 1, self.baseline)
        self.hashes = _write_deliverables(self.attempt.path)
        _strand(self.store)
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
            h.store.record_baseline(
                RUN_ID, NODE_ID, 2, wt.take_baseline(a2))

            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage(attempt_no=2)
            self.assertEqual(caught.exception.outcome, "SALVAGE_EMPTY_DELTA")
            self.assertFalse((a2.path / FILE_A).exists())


class SalvageMeasuresAgainstTheRecordedBaseline(unittest.TestCase):
    """The bracket's before-side is the provisioned tree, not the base commit.

    `git ls-tree` of the base commit is tracked paths only. The baseline
    deliberately is not: §8.3 keeps provisioned untracked content in scope so
    conjunct (2) can convict tampering with it. Rebuilding the baseline from
    the commit therefore reports every provisioned untracked path as a path
    the attempt added -- and where one is covered by a declared output, the
    permission check passes, the bytes are committed as the attempt's measured
    delta, and the signed receipt asserts their sha256. That is a receipt for
    work no attempt performed (§1.1 item 4).
    """

    def test_a_provisioned_path_under_a_declared_output_is_not_the_attempts_work(self):
        with _Harness(node=_globbed_node(),
                      provision=_provision_untracked) as h:
            self.assertIn(PROVISIONED, h.baseline)

            result = h.salvage()

            listed = _git(
                h.repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                result.output_sha).splitlines()
            self.assertEqual(listed, [FILE_A, FILE_B])
            self.assertNotIn(PROVISIONED, listed)
            self.assertNotIn(PROVISIONED, [row["path"] for row in result.files])
            payload = salvage.load_record(
                result.record_path, rc.seed_to_public_key(h.seed))
            self.assertNotIn(PROVISIONED,
                             [row["path"] for row in payload["files"]])
            self.assertEqual(payload["baseline_digest"], h.baseline_digest)

    def test_tampering_with_a_provisioned_path_still_convicts(self):
        """Conjunct (2) is what the reconstruction disabled: a provisioned
        path rewritten by the attempt read as `added`, which a glob is allowed
        to authorize. Measured against the recorded baseline it reads as
        `changed` on a path untracked at base, which nothing can authorize."""
        with _Harness(node=_globbed_node(),
                      provision=_provision_untracked) as h:
            (h.attempt.path / PROVISIONED).write_text("rewritten by the agent\n")

            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()

            self.assertEqual(caught.exception.outcome, "SALVAGE_PERMISSION_DENIED")
            self.assertTrue(any(
                PROVISIONED in violation
                for violation in caught.exception.fields["conjunct2"]))
            self.assertEqual(
                _git(h.repo, "rev-parse",
                     f"refs/heads/{wt.branch_name(RUN_ID, NODE_ID, 1)}"),
                h.base)

    def test_an_unrecorded_baseline_is_refused_and_signs_nothing(self):
        """Failing open here is the defect. An attempt row written before the
        baseline was persisted has no reconstructable before-side, so salvage
        refuses instead of approximating one."""
        with _Harness(node=_globbed_node(), provision=_provision_untracked,
                      record_baseline=False) as h:
            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()

            self.assertEqual(caught.exception.outcome, "SALVAGE_BASELINE_UNRECORDED")
            self.assertFalse(h.records.exists(),
                             "a refused salvage wrote a record directory")
            self.assertEqual(
                _git(h.repo, "rev-parse",
                     f"refs/heads/{wt.branch_name(RUN_ID, NODE_ID, 1)}"),
                h.base)
            attempt = h.store.get_attempt(RUN_ID, NODE_ID, 1)
            self.assertNotIn("salvage_output_sha", attempt.extra)

    def test_a_baseline_that_does_not_match_its_digest_is_refused(self):
        """The attempt row carries the digest; the table carries the bytes.
        Rewriting one without the other is a detectable substitution, not a
        new baseline."""
        with _Harness(node=_globbed_node(),
                      provision=_provision_untracked) as h:
            h.store.conn.execute(
                "UPDATE attempt_baselines SET inventory_json=?"
                " WHERE run_id=? AND node_id=? AND attempt_no=?",
                (json.dumps({}), RUN_ID, NODE_ID, 1))

            with self.assertRaises(salvage.SalvageRefused) as caught:
                h.salvage()

            self.assertEqual(caught.exception.outcome, "SALVAGE_BASELINE_CORRUPT")
            self.assertFalse(h.records.exists())


class RecordedBaselineLedgerTests(unittest.TestCase):
    """The store's own contract for the recorded baseline."""

    def test_the_baseline_round_trips_through_the_ledger(self):
        with _Harness(node=_globbed_node(),
                      provision=_provision_untracked) as h:
            read_back = h.store.attempt_baseline(RUN_ID, NODE_ID, 1)
            self.assertEqual(read_back, h.baseline)
            self.assertIn(PROVISIONED, read_back)
            self.assertEqual(
                h.store.get_attempt(RUN_ID, NODE_ID, 1)
                .extra[lc.ATTEMPT_BASELINE_DIGEST_KEY],
                h.baseline_digest)

    def test_a_second_different_baseline_is_refused(self):
        with _Harness(node=_globbed_node(),
                      provision=_provision_untracked) as h:
            with self.assertRaises(lc.BaselineCorrupt):
                h.store.record_baseline(RUN_ID, NODE_ID, 1, {})

    def test_recording_the_same_baseline_twice_is_accepted(self):
        with _Harness(node=_globbed_node(),
                      provision=_provision_untracked) as h:
            again = h.store.record_baseline(RUN_ID, NODE_ID, 1, h.baseline)
            self.assertEqual(again, h.baseline_digest)

    def test_an_attempt_with_no_baseline_row_raises_unrecorded(self):
        with _Harness(record_baseline=False) as h:
            with self.assertRaises(lc.BaselineUnrecorded):
                h.store.attempt_baseline(RUN_ID, NODE_ID, 1)


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
