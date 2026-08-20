"""A reviewer-side failure retries the reviewer, never the builder (issue #90).

`lane-p5-gap-policy` in run 2a44d226e75a4be391a14f02b78a6d25 ran four attempts
of one node. Every one of them produced an accepted envelope and a passing
post-node gate; three of the four committed. Not one of them was ever reviewed.
Each reviewer-side failure classified the *attempt* ENVIRONMENTAL, released it,
and the next attempt built a fresh worktree at the integration head and ran the
builder again — so the node blocked `ENVIRONMENTAL_BUDGET_EXHAUSTED` having
never received a verdict, after roughly 800 builder turns of which at most 250
were needed. `a1`'s commit `df98283` was checked by hand afterwards against the
plan's own verifier and was sound; nothing about it was invalidated by the
reviewer failing to report.

The gate counts across those attempts were 10, 12, 16, 19. That is the second
cost and the less obvious one: each restart replayed the retry guidance ledger,
so every cycle produced a *larger* implementation of the same requirement,
grown by failures that were never the builder's.

And it is not compensation for an unreliable reviewer. Over the same corpus the
reviewer completed 33 reviews — verdicts in both directions — against 7 that
took their receipt lock and never wrote a receipt. A rare, node-specific review
failure was discarding a builder commit nothing had ever faulted.

These tests hold the repair in place:

* the builder launches **once** when a reviewer stalls once, and the node still
  merges — the assertion that goes red if a reviewer-side failure ever again
  discards a committed builder attempt;
* the re-dispatch carries the attempt's **existing** output commit, not its
  base, and does not open a second attempt row;
* the decision is made of typed facts — a git object name read back from the
  attempt's own ref, a count of durable rows, and two numbers — and every
  reviewer-side failure lands a typed row carrying the window's own signal,
  which had zero readers and zero durable representations before this;
* the budget still ends the loop, at the reason the stall arm always wrote;
* and the receipt lock needs no reclamation, proven by executing the real
  store rather than by arguing about `flock`.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import finalization_window as fw  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402

from test_code_review import make_store  # noqa: E402
from test_scheduler import SchedulerFixture  # noqa: E402


def stall(signal: fw.FinalizationSignal = fw.FinalizationSignal.ACTOR_ABANDONED,
          elapsed_s: float = 12.0, session_id: str = "pane-1") -> cr.ReviewStalled:
    """A real `ReviewStalled`, carrying the typed fields production carries."""
    return cr.ReviewStalled(
        fw.ReviewerSession(route="omp", model="m", session_id=session_id),
        signal, elapsed_s)


class _Review:
    """A passing verdict, duck-typed as the scheduler consumes it."""

    passed = True
    findings: list = []
    advisories: list = []
    subject_digest = "digest-1"
    replayed = False

    def findings_text(self) -> str:
        return ""


class RedispatchTargetsTheReviewerTests(SchedulerFixture):
    """Driven through the real `Scheduler`, because the scheduler is where the
    defect lived. A test over `classify_review_stall` alone would have passed
    against the broken build."""

    def _dispatches(self, node_id: str = "a", attempt_no: int = 1):
        attempt = self.store.get_attempt("run1", node_id, attempt_no)
        return (attempt.extra or {}).get(rp.REVIEW_DISPATCH_KEY) or []

    def test_a_stalled_reviewer_does_not_re_run_the_builder(self):
        """The regression itself.

        Before the repair this ran the builder twice: the stall failed the
        attempt, the node returned to PENDING, and attempt 2 built a new
        worktree at the integration head from scratch. The committed tree of
        attempt 1 — measured, permission-checked, committed, and green at its
        post-node gate — was abandoned on its ref.
        """
        self.written = {"a": {"a.py": "A\n"}}
        seen = []

        def review_attempt(attempt, node, record, base_sha, output_sha):
            seen.append((record.attempt_no, base_sha, output_sha))
            if len(seen) == 1:
                raise stall()
            return _Review()

        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(review_attempt=review_attempt)).run()

        # One builder launch, not two. This is the assertion the issue is about.
        self.assertEqual(self.prompts["a"], [None])
        # One attempt row, so the attempt was not re-run from base.
        self.assertEqual(
            [a.attempt_no for a in self.store.attempts_for("run1", "a")], [1])
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_the_redispatch_carries_the_existing_commit_not_the_base(self):
        """Both dispatches judge the same tree, and it is the committed one.

        A re-dispatch against the base would be reviewing the code the builder
        started from, which is a verdict about nothing the node produced.
        """
        self.written = {"a": {"a.py": "A\n"}}
        seen = []

        def review_attempt(attempt, node, record, base_sha, output_sha):
            seen.append((record.attempt_no, base_sha, output_sha))
            if len(seen) == 1:
                raise stall()
            return _Review()

        self.schedule([self.agent("a")],
                      deps=self.deps(review_attempt=review_attempt)).run()

        self.assertEqual(len(seen), 2)
        first, second = seen
        self.assertEqual(first[0], second[0])          # same attempt
        self.assertEqual(first[2], second[2])          # same output commit
        self.assertNotEqual(second[2], second[1])      # and it is not the base
        # And it is the commit the node merged, so the tree the second reviewer
        # judged is the tree that landed. Asserted against the lifecycle row
        # rather than against the attempt ref, which the merge removes.
        self.assertEqual(second[2], self.store.get_node("run1", "a").output_sha)

    def test_every_reviewer_side_failure_lands_a_typed_row(self):
        """§3.6 B15 and §1.2 together.

        `ReviewStalled.signal` is a typed `FinalizationSignal` and until this
        row existed it had no reader and no durable representation anywhere:
        both window factories record stalls with `lambda _s, _sig, _e: None`,
        and the settle arm wrote one reason string for all four signals. So
        nothing on disk distinguished ACTOR_ABANDONED from PROCESS_DEAD,
        TURN_TIMEOUT, or WINDOW_TIMEOUT — and the re-dispatch decision had
        nothing typed to key on.
        """
        self.written = {"a": {"a.py": "A\n"}}
        calls = {"n": 0}

        def review_attempt(attempt, node, record, base_sha, output_sha):
            calls["n"] += 1
            if calls["n"] == 1:
                raise stall(fw.FinalizationSignal.TURN_TIMEOUT,
                            elapsed_s=7.5, session_id="pane-7")
            return _Review()

        self.schedule([self.agent("a")],
                      deps=self.deps(review_attempt=review_attempt)).run()

        rows = self._dispatches()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["signal"], fw.FinalizationSignal.TURN_TIMEOUT.value)
        self.assertEqual(row["route"], "omp")
        self.assertEqual(row["model"], "m")
        self.assertEqual(row["session_id"], "pane-7")
        self.assertEqual(row["elapsed_s"], 7.5)
        self.assertEqual(row["dispatch_no"], 1)
        # The subject is recorded on both sides: what was committed, and what
        # the attempt's own ref still held when the stall was classified. They
        # agree, which is what admitted the re-dispatch, and they name the
        # commit the node went on to merge.
        self.assertEqual(row["output_sha"], row["surviving_sha"])
        self.assertEqual(row["output_sha"],
                         self.store.get_node("run1", "a").output_sha)

    def test_the_budget_still_ends_the_loop_where_it_always_ended(self):
        """A reviewer that never reports is still a failed attempt eventually.

        The exhausted case must land exactly where the unconditional settle
        landed before — ENVIRONMENTAL, with the same reason — so an operator's
        existing query for it keeps finding it. What changed is that it costs
        the builder nothing until the re-dispatches are spent.
        """
        self.written = {"a": {"a.py": "A\n"}}
        dispatches = {"n": 0}

        def review_attempt(attempt, node, record, base_sha, output_sha):
            dispatches["n"] += 1
            raise stall()

        report = self.schedule(
            [self.agent("a")],
            config=self.config(environmental_retries=2, semantic_ceiling=1),
            deps=self.deps(review_attempt=review_attempt)).run()

        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        # The first attempt spent one dispatch plus its two re-dispatches
        # before it was released, and each one left a row.
        self.assertEqual(len(self._dispatches(attempt_no=1)), 3)
        self.assertGreaterEqual(dispatches["n"], 3)

        reader = lc.LifecycleReader.open(self.root / "lifecycle.db")
        try:
            reasons = [t.get("detail", {}).get("reason")
                       for t in reader.transitions("run1")
                       if t.get("reason") == "retry:ENVIRONMENTAL"]
        finally:
            reader.close()
        self.assertIn(rp.REVIEW_REDISPATCH_EXHAUSTED, reasons)

    def test_a_commit_the_attempt_ref_no_longer_holds_is_not_re_reviewed(self):
        """The subject of a re-review has to be provable.

        `commit_measured_delta` advances the attempt's ref by compare-and-swap,
        so the ref is the durable identity of what the builder produced. If it
        no longer holds the SHA the harness committed, re-dispatching would
        hand the reviewer something other than what this attempt's evidence
        chain names — worse than not re-reviewing at all.
        """
        self.written = {"a": {"a.py": "A\n"}}
        head = wt.integration_head(self.repo, "integration/run1")

        def review_attempt(attempt, node, record, base_sha, output_sha):
            subprocess.run(
                ["git", "update-ref",
                 "refs/heads/" + wt.branch_name("run1", node.node_id,
                                                record.attempt_no),
                 head],
                cwd=str(self.repo), check=True, capture_output=True)
            raise stall()

        report = self.schedule(
            [self.agent("a")],
            config=self.config(environmental_retries=0, semantic_ceiling=1),
            deps=self.deps(review_attempt=review_attempt)).run()

        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        rows = self._dispatches()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["output_sha"], rows[0]["surviving_sha"])
        reader = lc.LifecycleReader.open(self.root / "lifecycle.db")
        try:
            reasons = [t.get("detail", {}).get("reason")
                       for t in reader.transitions("run1")]
        finally:
            reader.close()
        self.assertIn(rp.REVIEW_OUTPUT_UNPROVEN, reasons)

    def test_a_stall_longer_than_the_remaining_window_is_not_re_dispatched(self):
        """Without this the repair converts a reviewer stall into a
        `NODE_TIMEOUT` and discards the same commit one horizon later, which is
        the failure it exists to stop."""
        self.written = {"a": {"a.py": "A\n"}}

        def review_attempt(attempt, node, record, base_sha, output_sha):
            raise stall(elapsed_s=10_000.0)

        report = self.schedule(
            [self.agent("a")],
            # Budget deliberately *not* exhausted: the point is that a stall
            # longer than the window's remainder is refused on its own terms,
            # not because there were no re-dispatches left to spend.
            config=self.config(node_timeout_s=60.0, environmental_retries=2,
                               semantic_ceiling=1),
            deps=self.deps(review_attempt=review_attempt)).run()

        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        reader = lc.LifecycleReader.open(self.root / "lifecycle.db")
        try:
            reasons = [t.get("detail", {}).get("reason")
                       for t in reader.transitions("run1")]
        finally:
            reader.close()
        self.assertIn(rp.REVIEW_REDISPATCH_NO_HEADROOM, reasons)

    def test_the_evidence_chain_is_complete_when_the_verdict_arrives_late(self):
        """§1.1 item 4, for a verdict produced on a later dispatch.

        The chain is a property of the *attempt*, and re-dispatching the
        reviewer changes nothing in it: the same envelope, the same worktree,
        the same red pre-gate and green post-gate, the same permission check,
        the same output commit, merged from the same ref. The dispatch rows sit
        beside that chain and say how many reviewers it took to get an answer;
        they are not part of it and do not stand in for any of it.
        """
        self.written = {"a": {"a.py": "A\n"}}
        calls = {"n": 0}

        def review_attempt(attempt, node, record, base_sha, output_sha):
            calls["n"] += 1
            if calls["n"] == 1:
                raise stall()
            return _Review()

        self.schedule([self.agent("a")],
                      deps=self.deps(review_attempt=review_attempt)).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.MERGED)
        self.assertTrue(node.output_sha)
        attempt = self.store.get_attempt("run1", "a", 1)
        # One attempt row, carrying the whole chain: the base it ran from, the
        # baseline its bracket measured, and the commit it published.
        self.assertEqual(
            [a.attempt_no for a in self.store.attempts_for("run1", "a")], [1])
        self.assertTrue(attempt.base_sha)
        self.assertTrue(
            self.store.attempt_baseline("run1", "a", 1) is not None)
        # The merged SHA descends from that attempt's own recorded base, and is
        # an ancestor of the integration head.
        self.assertTrue(wt.is_valid_output_commit(
            self.repo, node.output_sha, expected_base=attempt.base_sha))
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", node.output_sha,
             wt.integration_head(self.repo, "integration/run1")],
            cwd=str(self.repo), capture_output=True)
        self.assertEqual(merged.returncode, 0)


class RedispatchPolicyTests(unittest.TestCase):
    """`classify_review_stall` on its own — the arithmetic, without a run."""

    SHA = "a" * 40

    def facts(self, **kw) -> rp.ReviewStallFacts:
        base: Dict[str, Any] = dict(output_sha=self.SHA, surviving_sha=self.SHA,
                                    dispatches_spent=0, budget=2)
        base.update(kw)
        return rp.ReviewStallFacts(**base)

    def test_a_surviving_commit_under_budget_is_re_dispatched(self):
        self.assertIs(rp.classify_review_stall(self.facts()).outcome,
                      rp.ReviewDispatchOutcome.REDISPATCH)

    def test_an_attempt_with_no_output_commit_is_settled(self):
        """a5's shape, and it is the reason `output_sha` is checked rather than
        assumed: an attempt that never committed has nothing to re-review, and
        must not enter the re-dispatch path at all."""
        decision = rp.classify_review_stall(
            self.facts(output_sha=None, surviving_sha=None))
        self.assertIs(decision.outcome, rp.ReviewDispatchOutcome.SETTLE)
        self.assertEqual(decision.reason, rp.REVIEW_OUTPUT_UNPROVEN)

    def test_a_missing_ref_is_settled_rather_than_guessed_at(self):
        decision = rp.classify_review_stall(self.facts(surviving_sha=None))
        self.assertIs(decision.outcome, rp.ReviewDispatchOutcome.SETTLE)
        self.assertEqual(decision.reason, rp.REVIEW_OUTPUT_UNPROVEN)

    def test_the_budget_is_a_ceiling_not_a_suggestion(self):
        self.assertIs(
            rp.classify_review_stall(self.facts(dispatches_spent=2)).outcome,
            rp.ReviewDispatchOutcome.SETTLE)
        self.assertIs(
            rp.classify_review_stall(self.facts(dispatches_spent=1)).outcome,
            rp.ReviewDispatchOutcome.REDISPATCH)

    def test_unmeasured_headroom_is_not_zero_headroom(self):
        """An attempt row with no `started_at` — a ledger older than the
        column — reports `None`, and refusing every re-dispatch on it would
        read a missing measurement as a measurement of nothing left."""
        self.assertIs(
            rp.classify_review_stall(
                self.facts(elapsed_s=900.0, window_headroom_s=None)).outcome,
            rp.ReviewDispatchOutcome.REDISPATCH)

    def test_dispatch_rows_number_themselves_from_the_count(self):
        row = rp.review_dispatch_row(self.facts(dispatches_spent=2))
        self.assertEqual(row["dispatch_no"], 3)

    def test_a_row_written_before_this_key_existed_counts_zero(self):
        record = st.AttemptRecord(run_id="r", node_id="n", attempt_no=1,
                                  base_sha=self.SHA, extra={})
        self.assertEqual(rp.review_dispatches_spent(record), 0)
        self.assertEqual(rp.review_dispatches_spent(None), 0)


class ReceiptLockTests(unittest.TestCase):
    """The lock a stalled review leaves behind needs no reclaiming.

    The natural assumption is the opposite — the seven failed reviews in the
    corpus left a `<digest>.lock` and no receipt, which reads as a lock held by
    a corpse — and a re-dispatch that met a genuinely held lock would either
    block forever or race a second receipt against the first.

    It is not held. `ReceiptStore._locked` takes an `fcntl.flock` over an open
    descriptor for the body of a context manager; the kernel drops it when that
    descriptor closes, process death included. The file persists because
    nothing unlinks it, and it is created by `store.recover(...)` — the first
    statement in `review_attempt`, executed before a reviewer is launched at
    all, which is why two of the corpus's seven lock-only cases have no session
    and no pane: they were refused by the launcher milliseconds after the lock
    file appeared.

    Executed rather than argued, against the real store and the real driver.
    """

    def _handoff(self, digest: str) -> cr.ReviewHandoff:
        return cr.ReviewHandoff(
            subject_digest=digest, run_id="run1", node_id="build",
            node_kind="agent", instruction="Add the parser.",
            declared_outputs=["parser.py"], gate_command=["pytest"],
            gate_selector="tests/parser", base_sha="1" * 40,
            output_sha="2" * 40, diff="--- a\n+++ b\n",
            matrix=[{"check_id": "c", "object_id": "o"}], pair_count=1,
            report_path="/tmp/report.json",
            rubric=[{"check_id": "c", "question": "is it right?"}])

    def _stalling_window(self, launched: list):
        def factory(_matrix):
            def launch():
                launched.append(len(launched) + 1)
                return fw.ReviewerSession(route="omp", model="m",
                                          session_id="pane-%d" % len(launched))
            statuses = ["working", "idle"]
            return fw.FinalizationWindow(
                config=fw.FinalizationConfig(finalization_timeout_s=600.0,
                                             turn_timeout_s=300.0,
                                             poll_interval_s=0.001),
                launch=launch, poll_report=lambda: None,
                record_reviewer_session=lambda _s: None,
                kill=lambda _s: None,
                actor_status=lambda _s: (statuses.pop(0) if statuses else "idle"),
                transcript_record_count=lambda _s: 0)
        return factory

    def test_a_stalled_review_leaves_a_lock_file_that_blocks_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = make_store(root)
            digest = "c" * 64
            handoff = self._handoff(digest)
            objects = cr.review_objects(("a.py",), "2" * 40)
            launched: list = []
            factory = self._stalling_window(launched)

            with self.assertRaises(cr.ReviewStalled):
                cr.review_attempt(
                    subject_digest=digest, handoff=handoff, objects=objects,
                    rubric=cr.CODE_RUBRIC, store=store,
                    window_factory=factory,
                    occupancy_reader=lambda _s: 0.1, sleep=lambda _s: None)

            lock = root / "receipts" / (digest + ".lock")
            self.assertTrue(lock.is_file(), "the corpus's lock-only artefact")
            self.assertFalse(store.has(digest))

            # The second dispatch is not blocked by it: it reaches the window.
            with self.assertRaises(cr.ReviewStalled):
                cr.review_attempt(
                    subject_digest=digest, handoff=handoff, objects=objects,
                    rubric=cr.CODE_RUBRIC, store=store,
                    window_factory=factory,
                    occupancy_reader=lambda _s: 0.1, sleep=lambda _s: None)
            self.assertEqual(launched, [1, 2])

            # And the store is still writable through the same lock path, so a
            # dispatch that does report can publish its receipt.
            store.write(fin.Receipt(
                plan_digest=digest, rubric_version=cr.CODE_RUBRIC.version,
                verdict=fin.Verdict.PASS, cells=(),
                reviewer=fin.ReviewerIdentity(route="omp", model="m",
                                              session_id="pane-2"),
                created_at_epoch=1.0))
            self.assertTrue(store.has(digest))

            # Create-once holds afterwards: a further dispatch replays the one
            # receipt rather than launching a reviewer to race a second.
            def must_not_launch(_matrix):
                raise AssertionError("a published receipt launched a reviewer")

            outcome = cr.review_attempt(
                subject_digest=digest, handoff=handoff, objects=objects,
                rubric=cr.CODE_RUBRIC, store=store,
                window_factory=must_not_launch,
                occupancy_reader=lambda _s: 0.1)
            self.assertTrue(outcome.replayed)
            # The verdict stays attributable to the session that produced it.
            self.assertEqual(outcome.receipt.reviewer.session_id, "pane-2")


if __name__ == "__main__":
    unittest.main()
