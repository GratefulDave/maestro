"""Executable proof of the scheduler's shared vocabulary (§7.3, §7.5, §11.2).

This module is deliberately the smallest thing Step 6 can be built on: the
six states, the four run outcomes, the three retry classes, the stored block
reasons and their exits, the node model the scheduler consumes directly, and
the configuration whose liveness bound preflight enforces.

It exists as its own module for one reason. The scheduler, the retry policy,
and the watchdog are three separately-implemented pieces that must agree on
these names exactly; three private copies of one enum is the RC1 shape (§4)
this design convicts, and it would not be caught by any test either piece
could write about itself.

The tests below are the agreement, executed:

  §7.3   six states, two kinds of terminal, and the outcome set
  §7.3   UPSTREAM_BLOCKED is derived, so it is neither a state nor a reason
  §7.5   three retry classes, and the three failures that belong to none
  §11.3  every *stored* block reason admits a real exit, not just abandon
  §7.4   an agent node cannot be constructed without its own gate selector
  §7.3   a code node's acceptance is its exit code, so it carries no gate
  §11.2  T must exceed the greatest run-window timeout, or preflight refuses

Run with:  uv run adws/adw_test.py -k scheduler_types
"""

from __future__ import annotations

import sys
import unittest
from enum import Enum
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


# ── §7.3 states and the two kinds of terminal ───────────────────────────────


class StatesTests(unittest.TestCase):
    def test_exactly_seven_states(self):
        self.assertEqual(
            {s.value for s in st.NodeState},
            {
                "PENDING",
                "RUNNING",
                "VERIFIED",
                "ACCEPTED",
                "MERGED",
                "BLOCKED",
                "CANCELLED",
            },
        )

    def test_two_kinds_of_terminal_are_distinct(self):
        """MERGED, ACCEPTED, and CANCELLED are immutable; BLOCKED is not.

        ACCEPTED is terminal evidence for a derived review node, not a source
        merge. Nothing leaves the immutable set; an operator escape may leave
        BLOCKED.
        """
        self.assertEqual(
            set(st.ABSOLUTELY_TERMINAL),
            {st.NodeState.MERGED, st.NodeState.ACCEPTED, st.NodeState.CANCELLED},
        )

    def test_terminal_without_merge_matches_the_merge_protocol(self):
        """One representation of the cascade set, shared with §8.5's frontier.

        worktree.py already owns this tuple because the frontier computation
        needs it. A second private copy here would be two representations of
        one fact reconciled by convention (RC1), and it would drift silently:
        adding a state to one and not the other produces a node the scheduler
        treats as dead and the frontier still waits on.
        """
        self.assertEqual(
            tuple(s.value for s in st.TERMINAL_WITHOUT_MERGE),
            tuple(wt.TERMINAL_WITHOUT_MERGE),
        )

    def test_upstream_blocked_is_not_a_state(self):
        """§8.7 — derived, never stored, so the cascade stays reversible."""
        self.assertNotIn("UPSTREAM_BLOCKED", {s.value for s in st.NodeState})
        self.assertNotIn("UPSTREAM_BLOCKED", {r.value for r in st.BlockReason})

    def test_ready_and_merge_ready_are_not_states(self):
        """§7.3 — READY is a predicate and MERGE_READY is a query."""
        names = {s.value for s in st.NodeState}
        self.assertNotIn("READY", names)
        self.assertNotIn("MERGE_READY", names)
        self.assertNotIn("MERGING", names)


class RunOutcomeTests(unittest.TestCase):
    def test_outcome_set_is_closed(self):
        self.assertEqual(
            {o.value for o in st.RunOutcome},
            {"ACCEPTED", "BLOCKED", "CANCELLED", "STUCK"},
        )

    def test_blocked_is_the_residual_class(self):
        """§7.3 — the residual is named, so an unanticipated combination
        lands inside the set with a report rather than outside it."""
        self.assertIs(st.RESIDUAL_OUTCOME, st.RunOutcome.BLOCKED)


# ── §7.5 retry classes and the failures that belong to none ─────────────────


class RetryClassTests(unittest.TestCase):
    def test_exactly_three_classes(self):
        self.assertEqual(
            {c.value for c in st.RetryClass},
            {"SEMANTIC", "ENVIRONMENTAL", "LAUNCHER_TRANSIENT"},
        )

    def test_environmental_is_the_fail_closed_default(self):
        """§7.5 containment — an unclassified failure is still classified,
        and never as a verdict about the code under test."""
        self.assertIs(st.DEFAULT_RETRY_CLASS, st.RetryClass.ENVIRONMENTAL)

    def test_only_semantic_mutates_the_prompt(self):
        self.assertEqual(
            {c for c in st.RetryClass if st.mutates_prompt(c)}, {st.RetryClass.SEMANTIC}
        )

    def test_deterministic_reasons_classify_to_no_retry_class(self):
        """§7.5 — retry cannot change a deterministic fact about the plan
        evaluated against an unchanged base."""
        self.assertEqual(
            set(st.NON_RETRYABLE),
            {
                st.BlockReason.GATE_NOT_FALSIFIABLE,
                st.BlockReason.CODE_NODE_NO_EFFECT,
                st.BlockReason.PERMISSION_SCOPE_VIOLATION,
                st.BlockReason.DECLARED_OUTPUT_UNCOMMITTABLE,
                st.BlockReason.PRODUCED_SYMBOL_UNREFERENCED,
            },
        )


# ── §11.3 every stored block reason has a real exit ─────────────────────────


class BlockReasonExitTests(unittest.TestCase):
    def test_every_reason_admits_a_legal_exit(self):
        for reason in st.BlockReason:
            with self.subTest(reason=reason.value):
                self.assertTrue(st.exits_for(reason), f"{reason.value} is a dead end")

    def test_no_reason_is_satisfied_by_abandon_alone(self):
        """The non-vacuity §8.7 bought by deriving UPSTREAM_BLOCKED.

        `abandon` is a legal exit from every blocked state, so a property
        proved only by abandon proves a kill switch exists and nothing more.
        Every *stored* reason names a node that actually failed at something,
        so every stored reason has a repair as well as a kill.
        """
        for reason in st.BlockReason:
            with self.subTest(reason=reason.value):
                repairs = set(st.exits_for(reason)) - {st.Escape.ABANDON}
                self.assertTrue(repairs, f"{reason.value} admits only abandon")

    def test_semantic_exhaustion_is_the_forced_retry_case(self):
        """§7.5 — `retry --force` grants one attempt beyond K, never raises K."""
        self.assertIn(
            st.Escape.RETRY_FORCE,
            st.exits_for(st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED),
        )

    def test_non_retryable_reasons_do_not_offer_retry(self):
        """Re-running an agent cannot make a gate falsifiable, and re-running
        a deterministic command cannot write different paths."""
        for reason in st.NON_RETRYABLE:
            with self.subTest(reason=reason.value):
                offered = set(st.exits_for(reason))
                self.assertNotIn(st.Escape.RETRY, offered)
                self.assertNotIn(st.Escape.RETRY_FORCE, offered)

    def test_every_block_reason_is_mapped(self):
        """The completeness the other tests in this class assume.

        `test_every_reason_admits_a_legal_exit` proves the *value* is
        non-empty, which it can only do once the lookup has already succeeded;
        a member absent from `_EXITS` fails it with a lookup error rather than
        with the fact. `SEMANTIC_REFUSAL_REPEATED` shipped in the enum with no
        entry and nothing in production noticed, because nothing in production
        reads this table at all — the map is asserted about here and consulted
        nowhere else, so the suite is the only thing standing between an added
        member and a blocked node that declares no way out.

        Set equality, in both directions on purpose: a missing key is the
        defect that happened, and a stale key is the same defect run backwards
        — an escape declared for a reason nothing can store any more.
        """
        self.assertEqual(
            set(st._EXITS),
            set(st.BlockReason),
            "every stored block reason declares its exits, and only stored "
            "reasons appear in the table",
        )

    def test_repeated_semantic_refusal_does_not_offer_plain_retry(self):
        """The escape the block exists to withhold.

        The reason fires when two consecutive attempts produced the identical
        content-level refusal, and K is not exhausted when it does — so plain
        `retry` is admitted on budget and re-dispatches the identical node
        against the identical input, which is the loop this member was added
        to cut. `retry --force` is the same dispatch with an operator's
        signature on it, so it stays.
        """
        offered = set(st.exits_for(st.BlockReason.SEMANTIC_REFUSAL_REPEATED))
        self.assertNotIn(st.Escape.RETRY, offered)
        self.assertIn(st.Escape.RETRY_FORCE, offered)
        self.assertTrue(offered - {st.Escape.ABANDON}, "a kill is not a repair")

    def test_an_unmapped_reason_reads_as_a_build_defect(self):
        """The failure an added-and-unmapped member gets from now on.

        A bare `KeyError` at a dict subscript reprs the member and says
        nothing about what to do; the point of the reason existing at all is
        that a blocked operator is told their way out.
        """

        class Unmapped(str, Enum):
            NOT_IN_THE_TABLE = "NOT_IN_THE_TABLE"

        with self.assertRaises(st.UnmappedBlockReason) as caught:
            st.exits_for(Unmapped.NOT_IN_THE_TABLE)
        message = str(caught.exception)
        self.assertIn("NOT_IN_THE_TABLE", message)
        self.assertIn("_EXITS", message)
        self.assertIn("no entry", message)
        self.assertIsInstance(caught.exception, LookupError)

    def test_the_enum_derived_tuples_are_subsets_and_not_tables(self):
        """The sibling shapes, and why only one of them owes completeness.

        `_EXITS` is the module's only enum-keyed *table* — a structure whose
        contract is that it answers for every member — which is why a member
        can go missing from it. The other five module-level constants derived
        from an enum are deliberately partial: each names the subset of its
        enum for which some property holds, so a member's absence is the
        answer `no`, not a gap. What they can still get wrong is holding a
        value from the wrong enum, which is what this checks.
        """
        subsets = {
            "LANE_PHASE_TERMINAL": (st.LANE_PHASE_TERMINAL, st.LanePhase),
            "ABSOLUTELY_TERMINAL": (st.ABSOLUTELY_TERMINAL, st.NodeState),
            "TERMINAL_WITHOUT_MERGE": (st.TERMINAL_WITHOUT_MERGE, st.NodeState),
            "REOPENABLE_CANCEL_CAUSES": (
                st.REOPENABLE_CANCEL_CAUSES,
                st.CancelCause,
            ),
            "NON_RETRYABLE": (st.NON_RETRYABLE, st.BlockReason),
        }
        for name, (members, enum) in subsets.items():
            with self.subTest(constant=name):
                self.assertTrue(members, f"{name} is empty")
                for member in members:
                    self.assertIsInstance(member, enum)
                self.assertEqual(
                    len(set(members)), len(members), f"{name} repeats a member"
                )
                self.assertLess(
                    len(set(members)),
                    len(set(enum)),
                    f"{name} covers its whole enum; if that is now the "
                    "contract it is a table and owes the completeness test "
                    "above, not this one",
                )


# ── §7.3 / §7.4 the node model the scheduler consumes directly ──────────────


class PlanNodeTests(unittest.TestCase):
    def agent(self, **kw):
        base = dict(
            node_id="n1",
            kind=st.NodeKind.AGENT,
            depth=0,
            gate_command=("pytest",),
            gate_selector="tests/test_n1.py",
            outputs=("src/n1.py",),
            instruction="Build n1.",
        )
        base.update(kw)
        return st.PlanNode(**base)

    def code(self, **kw):
        base = dict(
            node_id="c1",
            kind=st.NodeKind.CODE,
            depth=0,
            command=("ruff", "format", "."),
            outputs=("src/*.py",),
        )
        base.update(kw)
        return st.PlanNode(**base)

    def test_agent_node_requires_its_own_selector(self):
        """§7.4's F3 correction, made structural rather than documented.

        An unscoped post-node gate is red for a sibling's absent work, so no
        node could verify and nothing could merge. Refusing at construction
        means the deadlock cannot be reintroduced by a caller passing None.
        """
        with self.assertRaises(ValueError):
            self.agent(gate_selector="")
        with self.assertRaises(ValueError):
            self.agent(gate_selector=None)

    def test_agent_node_requires_a_gate_command(self):
        with self.assertRaises(ValueError):
            self.agent(gate_command=())

    def test_code_node_carries_no_gate(self):
        """§7.3 — a code node's acceptance is its exit code. Clauses 1-3 do
        not apply to it, so a gate on one would be unevaluatable state."""
        with self.assertRaises(ValueError):
            self.code(gate_command=("pytest",), gate_selector="tests/")

    def test_code_node_requires_a_command(self):
        with self.assertRaises(ValueError):
            self.code(command=())

    def test_expects_changes_defaults_false(self):
        """§7.3 — the assertive "nothing broke" node is the common case and
        an empty diff is its normal result."""
        self.assertFalse(self.code().expects_changes)
        self.assertTrue(self.code(expects_changes=True).expects_changes)

    def test_agent_node_may_not_declare_expects_changes(self):
        """The clause is a code-node clause. Accepting it on an agent node
        would be a field nothing reads — a stub that looks implemented."""
        with self.assertRaises(ValueError):
            self.agent(expects_changes=True)

    def test_node_id_must_be_non_empty(self):
        with self.assertRaises(ValueError):
            self.agent(node_id="  ")

    def test_a_node_may_not_depend_on_itself(self):
        with self.assertRaises(ValueError):
            self.agent(needs=("n1",))



# ── §11.2 the liveness bound preflight enforces ─────────────────────────────


class SchedulerConfigTests(unittest.TestCase):
    def cfg(self, **kw):
        base = dict(
            concurrency=4,
            node_timeout_s=600.0,
            turn_timeout_s=120.0,
            final_acceptance_timeout_s=900.0,
            backstop_t_s=1800.0,
            semantic_ceiling=3,
        )
        base.update(kw)
        return st.SchedulerConfig(**base)

    def test_a_satisfied_bound_constructs(self):
        cfg = self.cfg()
        self.assertEqual(cfg.greatest_run_window_s, 900.0)

    def test_t_below_the_node_timeout_is_refused(self):
        """Below the first bound the backstop fires inside a healthy node's
        silent working gap (§11.2)."""
        with self.assertRaises(st.LivenessBoundUnsatisfied):
            self.cfg(node_timeout_s=3600.0)

    def test_t_below_the_final_acceptance_timeout_is_refused(self):
        """Below the second it fires inside a healthy final acceptance, and
        because resume re-runs acceptance that misfire is a livelock, not one
        lost run."""
        with self.assertRaises(st.LivenessBoundUnsatisfied):
            self.cfg(final_acceptance_timeout_s=3600.0)

    def test_t_equal_to_the_greatest_window_is_refused(self):
        """`T` must *exceed* the greatest window, not merely match it."""
        with self.assertRaises(st.LivenessBoundUnsatisfied):
            self.cfg(backstop_t_s=900.0)

    def test_the_refusal_prints_all_three_values(self):
        """§9.5 — LIVENESS_BOUND_UNSATISFIED with all three values printed,
        because an operator cannot fix a bound whose numbers are withheld."""
        with self.assertRaises(st.LivenessBoundUnsatisfied) as caught:
            self.cfg(backstop_t_s=100.0)
        message = str(caught.exception)
        self.assertIn("LIVENESS_BOUND_UNSATISFIED", message)
        for value in ("100.0", "600.0", "900.0"):
            self.assertIn(value, message)

    def test_the_finalization_timeout_takes_no_part(self):
        """§11.2 — no run exists at plan time, so it is not in the bound."""
        self.assertNotIn(
            "finalization", {f for f in st.SchedulerConfig.__dataclass_fields__}
        )

    def test_concurrency_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.cfg(concurrency=0)

    def test_semantic_ceiling_must_be_positive(self):
        """§7.5 — a ceiling of zero would block every agent node on its first
        semantic failure, which is a different design, not a configuration."""
        with self.assertRaises(ValueError):
            self.cfg(semantic_ceiling=0)


# ── §7.7 results adjudication vocabulary ────────────────────────────────────


class AdjudicationTests(unittest.TestCase):
    def test_four_outcomes(self):
        self.assertEqual(
            {a.value for a in st.Adjudication},
            {"ACCEPTED", "SUPERSEDED", "UNKNOWN_ATTEMPT", "SHA_MISMATCH"},
        )

    def test_a_result_carries_its_payload_in_every_outcome(self):
        """§7.7 — an adjudication cannot be recorded without its payload
        because they are the same row. This is the direct repair of a correct
        FAIL whose two real findings vanished with a byte-identical journal."""
        with self.assertRaises(ValueError):
            st.ResultRecord(
                node_id="n1", attempt_no=1, subject_sha="a" * 40, payload=None
            )

    def test_a_result_binds_the_attempt_it_names(self):
        result = st.ResultRecord(
            node_id="n1",
            attempt_no=2,
            subject_sha="b" * 40,
            payload={"verdict": "fail"},
        )
        self.assertEqual(result.key, ("n1", 2, "b" * 40))
        self.assertIsNone(result.adjudication)


if __name__ == "__main__":
    unittest.main()
