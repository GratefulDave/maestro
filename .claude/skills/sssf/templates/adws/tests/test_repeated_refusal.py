"""Executable proof that an identically-repeated refusal stops a node (§7.5).

The retry loop's premise is that a SEMANTIC retry is "genuinely new
instructions". Two production runs falsified it on 2026-08-27:
`lane-routing-chemical-tests` (run-d3bd665ce838456f989a15143f196710) produced
byte-identical TEST_STRENGTH_CONTROL_WRONG_REASON refusals on consecutive
semantic attempts, and `lane-wp6-tests` (run-8a200af7f9044ce7a11a51b6908f37e3)
produced byte-identical TESTS_NO_NEW_CASES refusals on its 2nd and 8th
attempts — nine attempts, zero convergence. The prompt could not change,
because the ledger appended the identical finding again (three copies in the
a9 prompt), so every re-dispatch was against provably unchanged inputs.

The invariant under test: **a node is not re-dispatched when the refusal it
just produced is identical, as a typed record, to the refusal its previous
content-level attempt produced.** It blocks `SEMANTIC_REFUSAL_REPEATED`,
naming the repeated refusal and the count. Four boundaries scope it:

  * identity requires a typed `refusal_code` — a coarse red gate ("gate
    exited 1") supports no identity claim and keeps its full budget;
  * only SEMANTIC rows enter the history — ENVIRONMENTAL and
    LAUNCHER_TRANSIENT retries are untouched, and infra noise between two
    identical refusals does not break the chain (the lane-wp6 shape);
  * the history honours the operator boundary the budgets honour, so
    `maestro retry --force` genuinely buys attempts;
  * the guidance ledger structurally refuses duplicate identical entries,
    so a prompt never carries the same verdict twice.

Run with:  uv run adw_test.py -k repeated_refusal
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest  # noqa: E402

from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules import verification as vf  # noqa: E402

from test_scheduler import SchedulerFixture, _git  # noqa: E402

REFUSAL = "TESTS_NO_NEW_CASES"
REFUSAL_TEXT = "no new collected case versus the parent commit"


def _semantic_verdict(code: str = REFUSAL, text: str = REFUSAL_TEXT):
    return vf.VerificationVerdict(
        verified=False,
        failed_clause=3,
        reason="{0}: {1}".format(code, text),
        retry_class=st.RetryClass.SEMANTIC,
        refusal_code=code,
    )


def _coarse_verdict():
    """A red gate: real, semantic, and carrying no refusal identity."""
    return vf.VerificationVerdict(
        verified=False, failed_clause=3, reason="gate exited 1"
    )


class RefusalRepetitionPolicyTests(unittest.TestCase):
    """The pure §7.5 predicate, no store and no scheduler."""

    def guidance(self, code=REFUSAL, text=REFUSAL_TEXT, clause=3):
        return rp.VerificationGuidance(
            reason="{0}: {1}".format(code, text),
            failed_clause=clause,
            refusal_code=code,
        )

    def test_second_identical_refusal_counts_two(self):
        current = self.guidance()
        self.assertEqual(rp.refusal_repetition((self.guidance(),), current), 2)
        self.assertGreaterEqual(2, rp.IDENTICAL_REFUSAL_LIMIT)

    def test_first_occurrence_counts_one(self):
        self.assertEqual(rp.refusal_repetition((), self.guidance()), 1)

    def test_a_different_refusal_breaks_the_run(self):
        history = (self.guidance(), self.guidance(code="TESTS_HOLLOW_AT_PARENT"))
        self.assertEqual(rp.refusal_repetition(history, self.guidance()), 1)

    def test_an_uncoded_refusal_never_repeats(self):
        """"gate exited 1" is true of every failing run; two of them prove
        nothing about whether the work was the same, so a refusal without a
        typed code supports no identity claim at all."""
        coarse = rp.VerificationGuidance(reason="gate exited 1", failed_clause=3)
        self.assertEqual(rp.refusal_repetition((coarse, coarse), coarse), 1)

    def test_uncoded_history_never_matches_a_coded_current(self):
        coarse = rp.VerificationGuidance(reason=REFUSAL, failed_clause=3)
        self.assertEqual(rp.refusal_repetition((coarse,), self.guidance()), 2 - 1)

    def test_run_of_three_counts_three(self):
        current = self.guidance()
        history = (self.guidance(), self.guidance())
        self.assertEqual(rp.refusal_repetition(history, current), 3)

    def test_tests_chain_refusals_carry_their_typed_code(self):
        """The stamped member, not a prose prefix, is the identity (§7.5)."""
        verdict = tc._refused(tc.TestsRefusal.NO_NEW_CASES, REFUSAL_TEXT)
        self.assertEqual(verdict.refusal_code, "TESTS_NO_NEW_CASES")
        strength = tc._strength_refused(
            "TEST_STRENGTH_CONTROL_WRONG_REASON", "observed: X"
        )
        self.assertEqual(
            strength.refusal_code, "TEST_STRENGTH_CONTROL_WRONG_REASON"
        )

    def test_failure_detail_carries_the_code(self):
        detail = sch._failure_detail(
            rp.Classification(retry_class=st.RetryClass.SEMANTIC),
            _semantic_verdict(),
        )
        self.assertEqual(detail["refusal_code"], REFUSAL)
        rebuilt = rp.verification_guidance(detail)
        self.assertEqual(rebuilt.refusal_code, REFUSAL)

    def test_guidance_payload_roundtrips_the_code(self):
        detail = sch._failure_detail(
            rp.Classification(retry_class=st.RetryClass.SEMANTIC),
            _semantic_verdict(),
        )
        payload = rp.guidance_extra_verification(detail)[rp.GUIDANCE_KEY]
        self.assertEqual(payload["refusal_code"], REFUSAL)
        self.assertEqual(
            rp._verification_from_payload(payload),
            rp.verification_guidance(detail),
        )

    def test_repeated_refusal_detail_names_verdict_and_count(self):
        current = self.guidance()
        detail = rp.repeated_refusal_detail({"clause": 3}, current, 2)
        self.assertEqual(detail["refusal_code"], REFUSAL)
        self.assertEqual(detail["identical_refusals"], 2)
        self.assertIn(REFUSAL_TEXT, detail["repeated_refusal"])
        self.assertEqual(detail["clause"], 3)


class GuidanceLedgerDedupeTests(unittest.TestCase):
    """A ledger never holds two equal entries, however it was built."""

    def entry(self, code=REFUSAL):
        return rp.VerificationGuidance(
            reason="{0}: {1}".format(code, REFUSAL_TEXT),
            failed_clause=3,
            refusal_code=code,
        )

    def test_with_verification_refuses_an_identical_entry(self):
        ledger = (
            rp.GuidanceLedger()
            .with_verification(self.entry())
            .with_verification(self.entry())
        )
        self.assertEqual(len(ledger.verification), 1)

    def test_direct_construction_dedupes_the_legacy_persistent_concat(self):
        """The observed duplication door: the same failure is durably recorded
        both as an attempt-row extra and as a lane retry spend, and
        `_refresh_lane_guidance` concatenates the two reconstructions."""
        one = (self.entry(),)
        ledger = rp.GuidanceLedger(verification=one + one)
        self.assertEqual(len(ledger.verification), 1)

    def test_distinct_entries_all_survive_in_order(self):
        a, b = self.entry(), self.entry(code="TESTS_HOLLOW_AT_PARENT")
        ledger = rp.GuidanceLedger(verification=(a, b))
        self.assertEqual(ledger.verification, (a, b))

    def test_a_recurring_entry_keeps_its_newest_position(self):
        """`_fit_surface` drops oldest-first, so the surviving copy of a
        recurring finding must sit where truncation reaches it last."""
        a, b = self.entry(), self.entry(code="TESTS_HOLLOW_AT_PARENT")
        ledger = rp.GuidanceLedger(verification=(a, b, a))
        self.assertEqual(ledger.verification, (b, a))

    def test_review_surface_dedupes_identically(self):
        finding = rp.ReviewFinding(
            check_id="C1", object_id="f.py", message="m", blocking=True
        )
        entry = rp.ReviewGuidance(subject_digest="sha1", findings=(finding,))
        ledger = rp.GuidanceLedger().with_review(entry).with_review(entry)
        self.assertEqual(len(ledger.review), 1)

    def test_rendered_prompt_names_the_verdict_once(self):
        """The a5-prompt defect: the same verdict rendered twice (and three
        times by a9) because both durable records replayed into the prompt."""

        class _Node:
            outputs = ("tests/test_x.py",)

        ledger = rp.GuidanceLedger(verification=(self.entry(), self.entry()))
        rendered = rp.render_guidance(_Node(), ledger)
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertEqual(rendered.count(REFUSAL_TEXT), 1)

    def test_rebuild_from_duplicate_attempt_rows_dedupes(self):
        payload = rp.guidance_extra_verification(
            {"clause": 3, "verdict": "{0}: {1}".format(REFUSAL, REFUSAL_TEXT),
             "refusal_code": REFUSAL}
        )
        attempts = [
            st.AttemptRecord(
                run_id="r", node_id="n", attempt_no=no, base_sha="b",
                extra=dict(payload),
            )
            for no in (1, 2)
        ]
        ledgers = rp.guidance_from_attempts(attempts)
        self.assertEqual(len(ledgers[("n", "b")].verification), 1)


class RefusalHistoryTests(unittest.TestCase):
    """The two durable history projections feed the same predicate."""

    def payload(self, code=REFUSAL):
        return rp.guidance_extra_verification(
            {"clause": 3, "verdict": "{0}: {1}".format(code, REFUSAL_TEXT),
             "refusal_code": code}
        )

    def test_attempt_history_is_scoped_and_ordered(self):
        rows = [
            st.AttemptRecord(run_id="r", node_id="n", attempt_no=2,
                             base_sha="b1", extra=self.payload()),
            st.AttemptRecord(run_id="r", node_id="n", attempt_no=1,
                             base_sha="b1",
                             extra=self.payload("TESTS_HOLLOW_AT_PARENT")),
            # A different base is different inputs — out of scope.
            st.AttemptRecord(run_id="r", node_id="n", attempt_no=3,
                             base_sha="b2", extra=self.payload()),
            # An infra attempt carries no guidance and never appears.
            st.AttemptRecord(run_id="r", node_id="n", attempt_no=4,
                             base_sha="b1", extra={}),
        ]
        history = rp.verification_refusals_from_attempts(rows, ("n", "b1"))
        self.assertEqual(
            [g.refusal_code for g in history],
            ["TESTS_HOLLOW_AT_PARENT", REFUSAL],
        )

    def test_attempt_history_honours_the_operator_floor(self):
        rows = [
            st.AttemptRecord(run_id="r", node_id="n", attempt_no=no,
                             base_sha="b1", extra=self.payload())
            for no in (1, 2, 3)
        ]
        self.assertEqual(
            len(rp.verification_refusals_from_attempts(rows, ("n", "b1"),
                                                       floor=2)),
            1,
        )

    def test_spend_history_keeps_semantic_rows_only(self):
        def spend(seq, retry_class, detail):
            return st.LaneRetrySpend(
                run_id="r", build_node_id="n", retry_class=retry_class,
                cycle_seq=seq, candidate_sha=None, detail=detail,
                created_at="t",
            )

        semantic = {"clause": 3,
                    "verdict": "{0}: {1}".format(REFUSAL, REFUSAL_TEXT),
                    "refusal_code": REFUSAL}
        history = rp.verification_refusals_from_spends([
            spend(1, st.LaneRetryClass.SEMANTIC, semantic),
            spend(2, st.LaneRetryClass.LAUNCHER_TRANSIENT,
                  {"reason": "LaunchFailed"}),
            spend(3, st.LaneRetryClass.ENVIRONMENTAL,
                  {"reason": "NODE_TIMEOUT"}),
            spend(4, st.LaneRetryClass.SEMANTIC, semantic),
        ])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0], history[1])
        # This is the lane-wp6 shape: with infra rows projected out, the two
        # identical refusals are consecutive and the repetition claim holds.
        self.assertEqual(rp.refusal_repetition(history[:-1], history[-1]), 2)


class RepeatedRefusalStopsTheLane(SchedulerFixture):
    """The retained-lane path: `_settle_failure` with a derived review."""

    def settle(self, scheduler, node, verdict,
               retry_class=st.RetryClass.SEMANTIC, launcher_failure=None):
        attempt_no = self.store.start_attempt(
            "run1", node.node_id,
            _git(self.integration, "rev-parse", "HEAD"),
        )
        record = self.store.get_attempt("run1", node.node_id, attempt_no)
        scheduler._settle_failure(
            node,
            rp.Classification(
                retry_class=retry_class, launcher_failure=launcher_failure
            ),
            verdict,
            record=record,
        )
        return self.store.get_node("run1", node.node_id)

    def scheduler_for(self, node):
        scheduler = self.schedule(
            [node], config=self.config(semantic_ceiling=5)
        )
        scheduler.project()
        return scheduler

    def block_detail(self, node_id):
        row = self.store.conn.execute(
            "SELECT detail_json FROM transitions WHERE run_id='run1'"
            " AND node_id=? AND reason='blocked:SEMANTIC_REFUSAL_REPEATED'"
            " ORDER BY id DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def test_second_identical_refusal_blocks_with_the_typed_cause(self):
        node = self.agent("a")
        scheduler = self.scheduler_for(node)
        first = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(first.state, st.NodeState.PENDING)
        second = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(second.state, st.NodeState.BLOCKED)
        self.assertIs(
            second.block_reason, st.BlockReason.SEMANTIC_REFUSAL_REPEATED
        )
        detail = self.block_detail("a")
        self.assertEqual(detail["refusal_code"], REFUSAL)
        self.assertEqual(detail["identical_refusals"], 2)
        self.assertIn(REFUSAL_TEXT, detail["repeated_refusal"])

    def test_infra_noise_between_identical_refusals_does_not_break_the_chain(self):
        """lane-wp6-tests: five infra rows sat between the two identical
        TESTS_NO_NEW_CASES refusals, and the node retried to attempt nine."""
        node = self.agent("a")
        scheduler = self.scheduler_for(node)
        self.settle(scheduler, node, _semantic_verdict())
        after_env = self.settle(
            scheduler, node, None, retry_class=st.RetryClass.ENVIRONMENTAL
        )
        self.assertIs(after_env.state, st.NodeState.PENDING)
        second = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(second.state, st.NodeState.BLOCKED)
        self.assertIs(
            second.block_reason, st.BlockReason.SEMANTIC_REFUSAL_REPEATED
        )

    def test_a_different_refusal_re_arms_the_loop(self):
        node = self.agent("a")
        scheduler = self.scheduler_for(node)
        self.settle(scheduler, node, _semantic_verdict())
        moved = self.settle(
            scheduler, node,
            _semantic_verdict(code="TESTS_HOLLOW_AT_PARENT",
                              text="2 new cases green at the parent"),
        )
        self.assertIs(moved.state, st.NodeState.PENDING)
        third = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(third.state, st.NodeState.PENDING)

    def test_a_coarse_refusal_keeps_its_full_budget(self):
        """Two consecutive "gate exited 1" rows are not an identity claim:
        blocking on them would silently shrink every red-gate loop to two
        attempts, which is the budget change this design forbids."""
        node = self.agent("a")
        scheduler = self.scheduler_for(node)
        for _ in range(2):
            lifecycle = self.settle(scheduler, node, _coarse_verdict())
            self.assertIs(lifecycle.state, st.NodeState.PENDING)

    def test_environmental_retries_are_untouched(self):
        node = self.agent("a")
        scheduler = self.scheduler_for(node)
        for _ in range(2):
            lifecycle = self.settle(
                scheduler, node, None, retry_class=st.RetryClass.ENVIRONMENTAL
            )
            self.assertIs(lifecycle.state, st.NodeState.PENDING)
            self.assertIsNone(lifecycle.block_reason)

    def test_launcher_transient_retries_are_untouched(self):
        node = self.agent("a")
        scheduler = self.scheduler_for(node)
        lifecycle = self.settle(
            scheduler, node, None,
            retry_class=st.RetryClass.LAUNCHER_TRANSIENT,
            launcher_failure=rp.LauncherFailure.TRANSPORT,
        )
        self.assertIs(lifecycle.state, st.NodeState.PENDING)
        self.assertIsNone(lifecycle.block_reason)

    def test_operator_retry_forgives_the_identity_chain(self):
        """`maestro retry --force` must genuinely buy an attempt: the floored
        history restarts, so the granted attempt runs, and only a further
        identical pair re-blocks."""
        node = self.agent("a")
        scheduler = self.scheduler_for(node)
        self.settle(scheduler, node, _semantic_verdict())
        blocked = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(blocked.state, st.NodeState.BLOCKED)
        # An escape is legal only against a declared run (§11.3).
        self.store.declare_outcome("run1")
        reopened = self.store.retry("run1", "a", force=True)
        self.assertIs(reopened.state, st.NodeState.PENDING)
        first_after = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(first_after.state, st.NodeState.PENDING)
        second_after = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(second_after.state, st.NodeState.BLOCKED)
        self.assertIs(
            second_after.block_reason,
            st.BlockReason.SEMANTIC_REFUSAL_REPEATED,
        )


class RepeatedRefusalStopsAFreshAttemptNode(SchedulerFixture):
    """The fresh-attempt path: no derived review, guidance-key history."""

    def settle(self, scheduler, node, verdict):
        attempt_no = self.store.start_attempt(
            "run1", node.node_id,
            _git(self.integration, "rev-parse", "HEAD"),
        )
        record = self.store.get_attempt("run1", node.node_id, attempt_no)
        with mock.patch.object(scheduler, "_review_for_build",
                               return_value=None):
            scheduler._settle_failure(
                node,
                rp.Classification(retry_class=st.RetryClass.SEMANTIC),
                verdict,
                record=record,
            )
        return self.store.get_node("run1", node.node_id)

    def test_second_identical_refusal_blocks(self):
        node = self.agent("a")
        scheduler = self.schedule([node],
                                  config=self.config(semantic_ceiling=5))
        scheduler.project()
        first = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(first.state, st.NodeState.PENDING)
        second = self.settle(scheduler, node, _semantic_verdict())
        self.assertIs(second.state, st.NodeState.BLOCKED)
        self.assertIs(
            second.block_reason, st.BlockReason.SEMANTIC_REFUSAL_REPEATED
        )

    def test_a_coarse_refusal_keeps_its_full_budget(self):
        node = self.agent("a")
        scheduler = self.schedule([node],
                                  config=self.config(semantic_ceiling=5))
        scheduler.project()
        for _ in range(2):
            lifecycle = self.settle(scheduler, node, _coarse_verdict())
            self.assertIs(lifecycle.state, st.NodeState.PENDING)


if __name__ == "__main__":
    unittest.main()
