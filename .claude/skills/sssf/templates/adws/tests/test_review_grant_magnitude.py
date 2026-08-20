"""Executable proof that the operator escape past a review ceiling has a
magnitude, and that the block says how big it has to be (#81, B10, §11.3).

`_review_ceiling_reached` compares a **cumulative** count — every
review-rejected attempt for the `(run_id, node_id)` pair, across every base,
never decreasing — against `review_ceiling + granted`. `retry --force` grants
exactly one, and a second `--force` is refused because the first moved the node
to PENDING and `LifecycleStore.retry` declares
`require_state=(BLOCKED, RUNNING)`. So the escape was capped at +1 in exactly
the situation that needs more than +1: the real node this issue was filed from
sat at seven review-rejected attempts against a ceiling of six and needed three.

Two halves, and both are needed — a grant nobody can size is as unreachable as
a grant that cannot exceed one:

  §11.3  `retry --grant N` grants N, `--force` still grants exactly 1
  B10    the block payload carries the cumulative count and the N to pass

Run with:  uv run adw_test.py -k review_grant_magnitude
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_code_review import FakeReview  # noqa: E402
from test_lifecycle import make_node, new_store  # noqa: E402
from test_scheduler import SchedulerFixture  # noqa: E402

BASE_SHA = "0" * 40

#: The run this issue was filed from: `lane-p4-enrichment-ordering` in
#: `run-2a44d226e75a4be391a14f02b78a6d25`, blocked with seven review-rejected
#: attempts against a ceiling of six.
STRANDED_ALREADY = 7
STRANDED_CEILING = 6


class CeilingProbe:
    """The scheduler's ceiling predicate over fabricated attempt rows — the
    same seam `test_code_review` uses, so the rule is exercised rather than
    restated."""

    def __init__(self, cfg, attempts):
        self.run_id = "run1"
        self.config = cfg
        self.deps = SimpleNamespace(
            store=SimpleNamespace(
                attempts_for=lambda run_id, node_id: tuple(attempts)))


def _rejected(node_id: str, attempt_no: int) -> st.AttemptRecord:
    return st.AttemptRecord(
        run_id="run1", node_id=node_id, attempt_no=attempt_no,
        base_sha=BASE_SHA, state=st.NodeState.PENDING,
        extra={rp.REVIEW_REJECTED_KEY: True})


def _blocked_node(store: lc.LifecycleStore, node_id: str = "a") -> None:
    """A node BLOCKED on an exhausted budget, in a run that has declared, so
    the escape verbs are legal against it."""
    store.create_run("run1", "d", [make_node(node_id, 0)])
    store.start_attempt("run1", node_id, base_sha="s1")
    store.mark_blocked("run1", node_id,
                       st.BlockReason.REVIEW_BUDGET_EXHAUSTED)
    store.declare_outcome("run1")


class GrantMagnitudeTests(unittest.TestCase):
    """§11.3 — the escape carries a magnitude, and `--force` still means one."""

    def test_a_grant_of_n_grants_exactly_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store)
            row = store.retry("run1", "a", grant=3)
            self.assertEqual(row.state, st.NodeState.PENDING)
            self.assertEqual(
                store.get_node("run1", "a").granted_extra_attempts, 3)

    def test_force_still_grants_exactly_one(self):
        """The existing meaning, unchanged. `--force` is a grant of one and
        every operator who has typed it must keep getting one."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store)
            store.retry("run1", "a", force=True)
            self.assertEqual(
                store.get_node("run1", "a").granted_extra_attempts, 1)

    def test_a_plain_retry_still_grants_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store)
            store.retry("run1", "a")
            self.assertEqual(
                store.get_node("run1", "a").granted_extra_attempts, 0)

    def test_repeating_force_cannot_supply_the_magnitude(self):
        """Why the magnitude has to ride one call: the first `--force` moves
        the node to PENDING and `require_state` refuses the second. That guard
        is doing real work and is not what this loosened."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store)
            store.retry("run1", "a", force=True)
            with self.assertRaises(lc.IllegalTransition):
                store.retry("run1", "a", force=True)
            self.assertEqual(
                store.get_node("run1", "a").granted_extra_attempts, 1)

    def test_force_and_grant_together_are_refused(self):
        """`--force` *is* a grant of one, so accepting both would leave the
        total ambiguous between N and N+1 — the arithmetic an operator sizing
        a grant cannot afford to guess at."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store)
            with self.assertRaises(lc.EscapeRefused):
                store.retry("run1", "a", force=True, grant=2)
            self.assertEqual(
                store.get_node("run1", "a").state, st.NodeState.BLOCKED)

    def test_a_negative_grant_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store)
            with self.assertRaises(lc.EscapeRefused):
                store.retry("run1", "a", grant=-1)
            self.assertEqual(
                store.get_node("run1", "a").state, st.NodeState.BLOCKED)

    def test_the_transition_records_the_magnitude_it_granted(self):
        """A grant of three and a grant of one are the same escape verb, so
        the audit row is where the two are told apart."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store)
            store.retry("run1", "a", grant=3)
            granting = [row for row in store.audit_transitions("run1", "a")
                        if row.get("reason") == st.Escape.RETRY_FORCE.value]
            self.assertEqual(len(granting), 1)
            self.assertEqual(granting[0]["detail"]["granted_extra_delta"], 3)

    def test_the_grant_reopens_a_node_past_its_ceiling(self):
        """The two halves meeting, on the numbers this issue was filed from.

        Seven review-rejected attempts against a ceiling of six: the next
        rejection is checked as `7 + 1 >= 6 + granted`, so a grant of one or
        two re-blocks the node after burning a whole attempt — a worktree, a
        pane, an agent run and a reviewer pass — and only three reopens it.
        """
        cfg = st.SchedulerConfig(
            concurrency=1, node_timeout_s=1.0, turn_timeout_s=1.0,
            final_acceptance_timeout_s=1.0, backstop_t_s=100.0,
            semantic_ceiling=3, review_ceiling=STRANDED_CEILING)
        rows = [_rejected("n", i) for i in range(STRANDED_ALREADY)]
        for insufficient in (0, 1, 2):
            self.assertTrue(
                sch.Scheduler._review_ceiling_reached(
                    CeilingProbe(cfg, rows), "n", insufficient),
                f"a grant of {insufficient} must not reopen the node")
        self.assertFalse(sch.Scheduler._review_ceiling_reached(
            CeilingProbe(cfg, rows), "n", 3))

        # And the store can actually issue that three, in one command, against
        # a node that is BLOCKED — which is the whole complaint.
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_node(store, "n")
            store.retry("run1", "n", grant=3)
            self.assertFalse(sch.Scheduler._review_ceiling_reached(
                CeilingProbe(cfg, rows), "n",
                store.get_node("run1", "n").granted_extra_attempts))


class GrantCliTests(unittest.TestCase):
    """The verb an operator actually types."""

    def _main(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = maestro.main(argv)
        return code, output.getvalue()

    def test_retry_grant_reopens_the_node_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(database)
            _blocked_node(store)
            store.close()

            code, _ = self._main(
                ["retry", "run1", "a", "--grant", "3", "--db", str(database)])
            self.assertEqual(code, 0)

            store = lc.LifecycleStore(database)
            self.addCleanup(store.close)
            node = store.get_node("run1", "a")
            self.assertEqual(node.state, st.NodeState.PENDING)
            self.assertEqual(node.granted_extra_attempts, 3)

    def test_retry_force_from_the_command_line_still_grants_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(database)
            _blocked_node(store)
            store.close()

            code, _ = self._main(
                ["retry", "run1", "a", "--force", "--db", str(database)])
            self.assertEqual(code, 0)

            store = lc.LifecycleStore(database)
            self.addCleanup(store.close)
            self.assertEqual(
                store.get_node("run1", "a").granted_extra_attempts, 1)

    def test_force_and_grant_are_refused_at_parse_time(self):
        parser = maestro.build_parser()
        # Acquitted first, so the refusal below is not the parser rejecting a
        # flag it never had.
        self.assertEqual(
            parser.parse_args(["retry", "run1", "a", "--grant", "3"]).grant, 3)
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["retry", "run1", "a", "--force", "--grant", "3"])

    def test_a_zero_grant_is_refused_at_parse_time(self):
        """A silently ignored magnitude would leave the node blocked under a
        transition claiming it had been retried."""
        parser = maestro.build_parser()
        self.assertEqual(
            parser.parse_args(["retry", "run1", "a", "--grant", "1"]).grant, 1)
        for refused in ("0", "-2", "two"):
            with self.subTest(grant=refused):
                with self.assertRaises(SystemExit), \
                        contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(
                        ["retry", "run1", "a", "--grant", refused])


class BlockPayloadTests(SchedulerFixture):
    """B10 — the block says how far past the ceiling the node went.

    Sized from a real run through the real scheduler rather than from
    fabricated rows, because the number that was missing is the one the
    blocking path itself holds.
    """

    def config(self, **kw):
        kw.setdefault("review_ceiling", 3)
        return super().config(**kw)

    def _block(self):
        review = FakeReview([False, False, False])
        self.written["build"] = {"build.py": "ok\n"}
        report = self.schedule([self.agent("build")],
                               deps=self.deps(review_attempt=review)).run()
        self.assertEqual(
            self.store.get_node("run1", "build").block_reason,
            st.BlockReason.REVIEW_BUDGET_EXHAUSTED)
        blocked = [row for row in self.store.audit_transitions("run1", "build")
                   if row.get("reason") ==
                   f"blocked:{st.BlockReason.REVIEW_BUDGET_EXHAUSTED.value}"]
        self.assertEqual(len(blocked), 1)
        return report, blocked[0]["detail"]

    def test_the_payload_names_the_cumulative_count(self):
        """`review_convergence` reports the findings the *current process*
        saw — it printed `[3]` on the run this issue came from while the
        cumulative count was 7. The count the ceiling is compared against is
        the one an operator needs, and it was in no output at all."""
        report, detail = self._block()
        self.assertEqual(detail["review_attempts_total"], 2)
        self.assertEqual(detail["review_ceiling"], 3)
        self.assertEqual(detail["granted_extra_attempts"], 0)
        # The per-process series is not that number and never was: it counts
        # findings per attempt, not attempts against a budget.
        self.assertEqual(report.review_convergence["build"], (1, 1, 1))

    def test_the_payload_names_a_grant_that_actually_reopens_the_node(self):
        """The required grant is the arithmetic, not its inputs: the count the
        decision read excludes the attempt being blocked, whose marker is
        written in the same transaction, so a grant sized off the raw count
        would be short by exactly one."""
        _, detail = self._block()
        required = detail["review_grant_required"]
        self.assertEqual(required, 2)

        scheduler = self.schedule([self.agent("build")])
        # Everything the payload offers short of the stated grant still
        # re-blocks, and the stated grant does not.
        for insufficient in range(required):
            self.assertTrue(
                scheduler._review_ceiling_reached("build", insufficient))
        self.assertFalse(scheduler._review_ceiling_reached("build", required))

    def test_the_payload_reaches_the_surface_it_is_diagnosed_from(self):
        """B15 — a field with zero readers is a build failure. `run status`
        projects each transition's detail onto the attempt it belongs to, and
        that projection is what carries these numbers to the operator."""
        _, detail = self._block()
        history = maestro._attempt_history(
            self.store.audit_transitions("run1"))
        payloads = [entry["detail"]
                    for (node_id, _), entries in history.items()
                    if node_id == "build"
                    for entry in entries
                    if "review_attempts_total" in (entry.get("detail") or {})]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["review_grant_required"],
                         detail["review_grant_required"])
        self.assertEqual(payloads[0]["review_attempts_total"],
                         detail["review_attempts_total"])

    def test_the_stated_grant_is_issuable_in_one_command(self):
        """End to end: block, read the payload, hand its number to `retry
        --grant`, and the node is PENDING with a budget that admits another
        review round."""
        _, detail = self._block()
        self.store.declare_outcome("run1")
        self.store.retry("run1", "build", grant=detail["review_grant_required"])

        node = self.store.get_node("run1", "build")
        self.assertEqual(node.state, st.NodeState.PENDING)
        self.assertFalse(
            self.schedule([self.agent("build")])._review_ceiling_reached(
                "build", node.granted_extra_attempts))


if __name__ == "__main__":
    unittest.main()
