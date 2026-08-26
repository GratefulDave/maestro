"""A node an operator accepted is distinguishable from one the run merged.

`maestro skip` writes `node_lifecycle.state = 'MERGED'` and an `output_sha`,
and it performs no merge. Nothing downstream told the two apart: `run status`
reported both identically, and in run-2a44d226e75a4be391a14f02b78a6d25 the
visualizer rendered `lane-p5-gap-policy` as MERGED with an output SHA while
every one of its attempts was CANCELLED or BLOCKED, no reviewer had ever
produced a verdict on it, and 928 lines merged on a gate run and an operator's
word. Reading the integration branch's git log — where a merged lane leaves a
merge commit and a skipped one leaves only the attempt commit — was the only
way to see the difference, which is exactly the reconstruction §1.2 forbids
(#93).

Two facts are asserted here and they are separate:

* `node_lifecycle.merge_cause`, the typed column a reader keys on, which
  §1.1 item 4's audit needs in the authority tier;
* the evidence record the skip transition carries, which says what the ledger
  could and could not show about that node's chain at the moment it was
  accepted.

And one migration property, which is the part most easily got wrong: a MERGED
row written before the column reads UNRECORDED and never SCHEDULER.

Run with:
    python -m pytest tests/test_skip_merge_provenance.py -o addopts= -q
"""

from __future__ import annotations

import contextlib
import io
import json
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


DEAD_PID = 2_000_000_000
RUN_ID = "run-2a44d226e75a4be391a14f02b78a6d25"
SKIPPED = "lane-p5-gap-policy"
MERGED = "lane-p2-manifest-import"


def make_node(node_id: str, depth: int = 0, needs=()) -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.CODE,
        depth=depth,
        needs=tuple(needs),
        command=("true",),
    )


def make_agent_node(node_id: str) -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.AGENT,
        depth=0,
        instruction=f"Build {node_id}.",
        gate_command=("pytest",),
        gate_selector=f"tests/test_{node_id}.py",
    )


def _init_git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "Test")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _stamp_dead_scheduler(store: lc.LifecycleStore) -> None:
    store.conn.execute(
        "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
        (DEAD_PID, lc.scheduler_host(), RUN_ID),
    )


def _blocked_unreviewed(store: lc.LifecycleStore, node_id: str, base_sha: str) -> None:
    """The shape a skip is actually taken against: blocked, never verified."""
    store.start_attempt(RUN_ID, node_id, base_sha=base_sha)
    store.mark_blocked(RUN_ID, node_id, st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)


class MergeCauseVocabularyTests(unittest.TestCase):
    """`merge_cause_label` is the one derivation, and it has four answers."""

    def test_a_scheduler_merge_reads_scheduler(self):
        self.assertEqual(
            st.merge_cause_label(st.NodeState.MERGED, st.MergeCause.SCHEDULER),
            "SCHEDULER",
        )

    def test_an_operator_accepted_merge_reads_operator_accepted(self):
        self.assertEqual(
            st.merge_cause_label(st.NodeState.MERGED, st.MergeCause.OPERATOR_ACCEPTED),
            "OPERATOR_ACCEPTED",
        )

    def test_a_merged_row_with_no_recorded_cause_reads_unrecorded(self):
        """The migration invents no facts. This is the whole property.

        A ledger written before the column has MERGED rows whose provenance
        nobody recorded, and the one answer that must never come back for
        them is SCHEDULER — that would have every pre-existing row assert an
        evidence chain nobody checked.
        """
        self.assertEqual(
            st.merge_cause_label(st.NodeState.MERGED, None), st.MERGE_CAUSE_UNRECORDED
        )
        self.assertNotEqual(
            st.merge_cause_label(st.NodeState.MERGED, None),
            st.MergeCause.SCHEDULER.value,
        )

    def test_a_node_that_is_not_merged_has_no_provenance(self):
        for state in (
            st.NodeState.PENDING,
            st.NodeState.RUNNING,
            st.NodeState.VERIFIED,
            st.NodeState.BLOCKED,
            st.NodeState.CANCELLED,
        ):
            self.assertIsNone(st.merge_cause_label(state, None), state)

    def test_operator_accepted_is_the_unevidenced_cause(self):
        self.assertIn(st.MergeCause.OPERATOR_ACCEPTED, st.UNEVIDENCED_MERGE_CAUSES)
        self.assertNotIn(st.MergeCause.SCHEDULER, st.UNEVIDENCED_MERGE_CAUSES)


class MergeCauseIsStoredTests(unittest.TestCase):
    """The distinction is a typed column, not a reading of the git log."""

    def test_mark_merged_stamps_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(MERGED)])
            store.start_attempt(RUN_ID, MERGED, base_sha="a" * 40)
            store.mark_verified(RUN_ID, MERGED, output_sha="b" * 40)
            store.mark_merged(RUN_ID, MERGED)
            stored = store.conn.execute(
                "SELECT state, merge_cause FROM node_lifecycle"
                " WHERE run_id=? AND node_id=?",
                (RUN_ID, MERGED),
            ).fetchone()
            self.assertEqual(stored[0], st.NodeState.MERGED.value)
            self.assertEqual(stored[1], st.MergeCause.SCHEDULER.value)
            store.close()

    def test_skip_stamps_operator_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(SKIPPED)])
            _blocked_unreviewed(store, SKIPPED, sha)
            store.declare_outcome(RUN_ID)
            _stamp_dead_scheduler(store)

            row = store.skip(RUN_ID, SKIPPED, accept_sha=sha, repo_path=repo)

            self.assertIs(row.state, st.NodeState.MERGED)
            stored = store.conn.execute(
                "SELECT merge_cause FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (RUN_ID, SKIPPED),
            ).fetchone()
            self.assertEqual(stored[0], st.MergeCause.OPERATOR_ACCEPTED.value)
            store.close()

    def test_the_two_merged_nodes_are_distinguishable_in_the_projection(self):
        """The acceptance item, asserted where an operator actually reads it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            store.create_run(RUN_ID, "d" * 64, [make_node(MERGED), make_node(SKIPPED)])
            store.start_attempt(RUN_ID, MERGED, base_sha=sha)
            store.mark_verified(RUN_ID, MERGED, output_sha=sha)
            store.mark_merged(RUN_ID, MERGED)
            _blocked_unreviewed(store, SKIPPED, sha)
            store.declare_outcome(RUN_ID)
            _stamp_dead_scheduler(store)
            store.skip(RUN_ID, SKIPPED, accept_sha=sha, repo_path=repo)
            store.close()

            reader = lc.LifecycleReader.open(db)
            by_id = {node.node_id: node for node in reader.nodes(RUN_ID)}
            reader.close()

            self.assertIs(by_id[MERGED].state, st.NodeState.MERGED)
            self.assertIs(by_id[SKIPPED].state, st.NodeState.MERGED)
            self.assertEqual(by_id[MERGED].merge_provenance, "SCHEDULER")
            self.assertEqual(by_id[SKIPPED].merge_provenance, "OPERATOR_ACCEPTED")
            self.assertNotEqual(
                by_id[MERGED].merge_provenance, by_id[SKIPPED].merge_provenance
            )


class LedgerOlderThanTheColumnTests(unittest.TestCase):
    """An existing MERGED row reads "we cannot tell", never "really merged"."""

    def _ledger_without_the_column(self, tmp: Path) -> Path:
        """A ledger with the row present and the column absent.

        Built by dropping the column from a real ledger rather than by
        hand-writing an old schema, so the test cannot drift away from what
        `SCHEMA` actually produces.
        """
        db = tmp / "old.sqlite3"
        store = lc.LifecycleStore(db)
        store.create_run(RUN_ID, "d" * 64, [make_node(MERGED)])
        store.start_attempt(RUN_ID, MERGED, base_sha="a" * 40)
        store.mark_verified(RUN_ID, MERGED, output_sha="b" * 40)
        store.mark_merged(RUN_ID, MERGED)
        store.conn.execute("ALTER TABLE node_lifecycle DROP COLUMN merge_cause")
        store.conn.commit()
        store.close()
        return db

    def test_the_read_only_projection_reads_it_as_unrecorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._ledger_without_the_column(Path(tmp))
            reader = lc.LifecycleReader.open(db)
            node = reader.nodes(RUN_ID)[0]
            reader.close()
            self.assertIs(node.state, st.NodeState.MERGED)
            self.assertIsNone(node.merge_cause)
            self.assertEqual(node.merge_provenance, st.MERGE_CAUSE_UNRECORDED)

    def test_the_read_only_projection_does_not_refuse_the_old_ledger(self):
        """`mode=ro` cannot migrate, and must not refuse either."""
        with tempfile.TemporaryDirectory() as tmp:
            db = self._ledger_without_the_column(Path(tmp))
            reader = lc.LifecycleReader.open(db)
            self.assertEqual(len(reader.nodes(RUN_ID)), 1)
            reader.close()

    def test_opening_the_old_ledger_for_write_adds_the_column(self):
        """And the row it migrates still reads UNRECORDED afterwards.

        The migration adds a nullable column; it does not backfill one. A
        backfill would be the invented fact — the ledger has no record of how
        that node reached MERGED and nothing can recover it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = self._ledger_without_the_column(Path(tmp))
            store = lc.LifecycleStore(db)
            self.assertIn(
                "merge_cause", lc._table_columns(store.conn, "node_lifecycle")
            )
            stored = store.conn.execute(
                "SELECT merge_cause FROM node_lifecycle WHERE run_id=? AND node_id=?",
                (RUN_ID, MERGED),
            ).fetchone()
            store.close()
            self.assertIsNone(stored[0])

            reader = lc.LifecycleReader.open(db)
            node = reader.nodes(RUN_ID)[0]
            reader.close()
            self.assertEqual(node.merge_provenance, st.MERGE_CAUSE_UNRECORDED)


class SkipRecordsTheEvidenceGapTests(unittest.TestCase):
    """Acceptance item 2 — the absent chain is findable afterwards."""

    def _skip_transition(self, store: lc.LifecycleStore, node_id: str):
        rows = [
            row
            for row in store.audit_transitions(RUN_ID)
            if row.get("node_id") == node_id
            and row.get("reason") == st.Escape.SKIP.value
        ]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]["detail"][lc.MERGE_EVIDENCE_KEY]

    def test_an_unreviewed_node_records_that_it_never_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(SKIPPED)])
            _blocked_unreviewed(store, SKIPPED, sha)
            store.declare_outcome(RUN_ID)
            _stamp_dead_scheduler(store)
            store.skip(RUN_ID, SKIPPED, accept_sha=sha, repo_path=repo)

            evidence = self._skip_transition(store, SKIPPED)
            self.assertFalse(evidence["verified_ever"])
            self.assertEqual(evidence["verified_transitions"], 0)
            self.assertEqual(evidence["review_rejections"], 0)
            self.assertEqual(evidence["attempts_recorded"], 1)
            self.assertEqual(
                evidence["block_reason"], st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED.value
            )
            store.close()

    def test_review_rejections_are_counted_from_candidate_reviews(self):
        """A reviewer that looked and rejected is not the same as none."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_agent_node(SKIPPED)])
            store.ensure_derived_review_node(
                RUN_ID, SKIPPED, depth=1, downstream_needs=()
            )
            candidates = ("a" * 40, "b" * 40)
            for index, candidate_sha in enumerate(candidates, 1):
                store.start_attempt(RUN_ID, SKIPPED, base_sha=sha)
                store.publish_candidate(
                    RUN_ID,
                    SKIPPED,
                    candidate_sha=candidate_sha,
                    parent_candidate_sha=(candidates[index - 2] if index > 1 else None),
                    builder_generation=index,
                    ancestry_validator=lambda _parent, _child: True,
                )
                store.begin_review(
                    RUN_ID, f"{SKIPPED}::review", candidate_sha, reviewer_generation=1
                )
                store.mark_review_dispatched(
                    RUN_ID,
                    f"{SKIPPED}::review",
                    candidate_sha,
                    reviewer_generation=1,
                )
                store.reject_and_create_handoff(
                    RUN_ID,
                    f"{SKIPPED}::review",
                    candidate_sha,
                    reviewer_generation=1,
                    builder_generation=index,
                    review_digest=f"{index}" * 64,
                    receipt_path=f"/tmp/review-{index}.json",
                    findings=(
                        {
                            "check_id": "diff.correctness",
                            "grade": "error",
                            "message": "candidate rejected",
                        },
                    ),
                )
                if index == 1:
                    store.fail_attempt(RUN_ID, SKIPPED, st.RetryClass.SEMANTIC)
                else:
                    store.mark_blocked(
                        RUN_ID, SKIPPED, st.BlockReason.REVIEW_BUDGET_EXHAUSTED
                    )
            store.declare_outcome(RUN_ID)
            _stamp_dead_scheduler(store)
            store.skip(RUN_ID, SKIPPED, accept_sha=sha, repo_path=repo)

            evidence = self._skip_transition(store, SKIPPED)
            self.assertEqual(evidence["review_rejections"], 2)
            self.assertEqual(evidence["attempts_recorded"], 2)
            self.assertFalse(evidence["verified_ever"])
            store.close()

    def test_a_node_that_verified_before_it_blocked_records_that_it_did(self):
        """The field discriminates rather than always reading False.

        A node that verified and then blocked at merge time — §8.7's conflict
        — reaches `skip` with the machine's own chain intact, and the record
        must say so or it says nothing at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(SKIPPED)])
            store.start_attempt(RUN_ID, SKIPPED, base_sha=sha)
            store.mark_verified(RUN_ID, SKIPPED, output_sha=sha)
            store.mark_blocked(RUN_ID, SKIPPED, st.BlockReason.MERGE_CONFLICT)
            store.declare_outcome(RUN_ID)
            _stamp_dead_scheduler(store)
            store.skip(RUN_ID, SKIPPED, accept_sha=sha, repo_path=repo)

            evidence = self._skip_transition(store, SKIPPED)
            self.assertTrue(evidence["verified_ever"])
            self.assertEqual(evidence["verified_transitions"], 1)
            self.assertEqual(
                evidence["block_reason"], st.BlockReason.MERGE_CONFLICT.value
            )
            store.close()

    def test_a_scheduler_merge_writes_no_evidence_record(self):
        """It has a chain; the record exists to name an absent one."""
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(MERGED)])
            store.start_attempt(RUN_ID, MERGED, base_sha="a" * 40)
            store.mark_verified(RUN_ID, MERGED, output_sha="b" * 40)
            store.mark_merged(RUN_ID, MERGED)
            for row in store.audit_transitions(RUN_ID):
                self.assertNotIn(lc.MERGE_EVIDENCE_KEY, row.get("detail") or {})
            store.close()


class RunStatusReportsTheDifferenceTests(unittest.TestCase):
    """Every read verb, which is where the operator was misled."""

    def _progress(self, tmp: Path):
        repo = tmp / "repo"
        repo.mkdir()
        sha = _init_git_repo(repo)
        db = tmp / "lifecycle.db"
        store = lc.LifecycleStore(db)
        store.create_run(RUN_ID, "d" * 64, [make_node(MERGED), make_node(SKIPPED)])
        store.start_attempt(RUN_ID, MERGED, base_sha=sha)
        store.mark_verified(RUN_ID, MERGED, output_sha=sha)
        store.mark_merged(RUN_ID, MERGED)
        _blocked_unreviewed(store, SKIPPED, sha)
        store.declare_outcome(RUN_ID)
        _stamp_dead_scheduler(store)
        store.skip(RUN_ID, SKIPPED, accept_sha=sha, repo_path=repo)
        store.close()

        reader = lc.LifecycleReader.open(db)
        try:
            record = reader.run(RUN_ID)
            return maestro._run_progress(
                reader, record, SimpleNamespace(plan_digests={})
            )
        finally:
            reader.close()

    def test_the_json_projection_carries_the_cause_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = self._progress(Path(tmp))
            by_id = {node["node_id"]: node for node in progress["nodes"]}
            self.assertEqual(by_id[MERGED]["state"], "MERGED")
            self.assertEqual(by_id[SKIPPED]["state"], "MERGED")
            self.assertEqual(by_id[MERGED]["merge_cause"], "SCHEDULER")
            self.assertEqual(by_id[SKIPPED]["merge_cause"], "OPERATOR_ACCEPTED")
            # And the payload survives a round trip through the wire format
            # `run status --json` actually prints.
            self.assertEqual(
                json.loads(json.dumps(progress, sort_keys=True))["nodes"][0].get(
                    "merge_cause"
                )
                is not None,
                True,
            )

    def test_the_json_projection_carries_the_evidence_only_where_it_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = self._progress(Path(tmp))
            by_id = {node["node_id"]: node for node in progress["nodes"]}
            self.assertIsNone(by_id[MERGED]["merge_evidence"])
            evidence = by_id[SKIPPED]["merge_evidence"]
            self.assertFalse(evidence["verified_ever"])
            self.assertEqual(evidence["review_rejections"], 0)

    def test_the_human_render_says_operator_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rendered = maestro._render_progress(self._progress(Path(tmp)))
            self.assertIn("operator-accepted", rendered)
            self.assertIn("OPERATOR-ACCEPTED", rendered)
            self.assertIn("never reached VERIFIED", rendered)
            self.assertIn("evidence chain not established by this run", rendered)

    def test_the_human_render_leaves_a_run_merged_node_alone(self):
        """The ordinary case gains no annotation and no new vocabulary."""
        with tempfile.TemporaryDirectory() as tmp:
            rendered = maestro._render_progress(self._progress(Path(tmp)))
            merged_line = [
                line
                for line in rendered.splitlines()
                if line.strip().startswith(MERGED) and "MERGED" in line
            ]
            self.assertTrue(merged_line, rendered)
            self.assertIn("output ", merged_line[0])
            self.assertNotIn("operator-accepted", merged_line[0])

    def test_the_run_status_verb_prints_it(self):
        """Driven through the CLI handler, not just the projection."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _init_git_repo(repo)
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            store.create_run(RUN_ID, "d" * 64, [make_node(SKIPPED)])
            _blocked_unreviewed(store, SKIPPED, sha)
            store.declare_outcome(RUN_ID)
            _stamp_dead_scheduler(store)
            store.skip(RUN_ID, SKIPPED, accept_sha=sha, repo_path=repo)
            store.close()

            args = SimpleNamespace(
                db=str(db),
                run_id=RUN_ID,
                as_json=True,
                plan_digests={},
                repository_state=None,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = maestro._run_status(args)
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            node = payload["nodes"][0]
            self.assertEqual(node["merge_cause"], "OPERATOR_ACCEPTED")
            self.assertFalse(node["merge_evidence"]["verified_ever"])


class UnrecordedRendersAsUnrecordedTests(unittest.TestCase):
    """A pre-existing MERGED row is never displayed as a run merge."""

    def test_the_detail_column_says_the_provenance_is_unrecorded(self):
        self.assertIn(
            "unrecorded", maestro._merge_cause_prefix(st.MERGE_CAUSE_UNRECORDED)
        )

    def test_the_detail_column_marks_an_operator_accepted_node(self):
        self.assertIn(
            "operator-accepted",
            maestro._merge_cause_prefix(st.MergeCause.OPERATOR_ACCEPTED.value),
        )

    def test_a_run_merged_node_keeps_the_wording_it_had(self):
        self.assertEqual(
            maestro._merge_cause_prefix(st.MergeCause.SCHEDULER.value), "output "
        )
        self.assertEqual(maestro._merge_cause_prefix(None), "output ")


if __name__ == "__main__":
    unittest.main()
