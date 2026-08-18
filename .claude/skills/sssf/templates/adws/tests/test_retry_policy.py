"""Executable proof of §7.5's retry classification (`adw_modules/retry_policy.py`).

Retry classification is pure policy: it takes a structural description of a
failed attempt and returns a classification. It owns no store, runs nothing,
and talks to no git process directly — every fact it reads is passed in.

The tests below are the agreement, executed:

  §7.5   three classes, mutually exclusive, classified structurally
  §7.5   no report can ever be SEMANTIC
  §7.5   the classifier's own code never compares against process output text
  §7.5   only git's documented not-found exit code means "object absent"
  §7.5   an unclassified exception defaults ENVIRONMENTAL, fail-closed
  §7.5   no infra fault decrements the semantic budget
  §7.3   three failures classify to no retry class at all, not ENVIRONMENTAL
  §7.5   an agent node's clause-4 failure is SEMANTIC, a code node's is not
  §7.5   the semantic budget: prompt-mutation scope vs. the cumulative ceiling
  §7.5   `retry --force` grants exactly one attempt beyond the ceiling

Run with:  uv run adw_test.py -k retry_policy
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


def make_cfg(**overrides):
    defaults = dict(concurrency=2, node_timeout_s=60.0, turn_timeout_s=30.0,
                    final_acceptance_timeout_s=60.0, backstop_t_s=600.0,
                    semantic_ceiling=3)
    defaults.update(overrides)
    return st.SchedulerConfig(**defaults)


def make_attempt(node_id, base_sha, attempt_no, retry_class, run_id="r1"):
    return st.AttemptRecord(run_id=run_id, node_id=node_id, attempt_no=attempt_no,
                            base_sha=base_sha, retry_class=retry_class)


class CeilingProbe:
    """A `Scheduler` reduced to what `_semantic_ceiling_reached` reads.

    §7.5's cumulative ceiling has exactly one enforcer, and it is that method.
    A pure `retry_policy.semantic_budget_exhausted` stated the same rule from
    the outside, had no production caller, and disagreed with the enforcer by
    one: it counted only the attempt rows that already existed, while the
    scheduler also counts the attempt failing right now, whose row is written
    by the very call the decision gates. Testing the unused copy proved
    nothing about a run, so it was deleted and these tests were re-pointed
    here — the production method is invoked unbound against this object, so
    what executes is the rule the scheduler executes.

    It reads three things: `run_id`, `config`, and `deps.store.attempts_for`.
    """

    def __init__(self, cfg, attempts):
        self.run_id = "r1"
        self.config = cfg
        self.deps = SimpleNamespace(
            store=SimpleNamespace(
                attempts_for=lambda run_id, node_id: tuple(attempts)))


def ceiling_reached(cfg, node_id, attempts, granted=0):
    """§7.5's ceiling as production evaluates it, counting the in-flight
    attempt. `n` stored SEMANTIC rows means this is attempt `n + 1`."""
    return sch.Scheduler._semantic_ceiling_reached(
        CeilingProbe(cfg, attempts), node_id, granted)


# ── §7.5 classify() — structural, never lexical ─────────────────────────────

class ClassifyTests(unittest.TestCase):

    def test_no_report_is_never_semantic(self):
        """§7.5 — the structural answer to an exit code carrying two meanings.
        A nonzero exit with no report classifies ENVIRONMENTAL regardless of
        what the process printed — there is no field here for stdout/stderr
        text to even arrive through."""
        signal = rp.FailureSignal(node_kind=st.NodeKind.CODE, exit_code=1, report=None)
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.ENVIRONMENTAL)
        self.assertIsNone(result.block_reason)

    def test_unparseable_report_is_never_semantic(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.AGENT,
                                  report=rp.ReportOutcome(parsed=False))
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.ENVIRONMENTAL)

    def test_parseable_failing_report_is_semantic(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.AGENT,
                                  report=rp.ReportOutcome(parsed=True, failed=True))
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.SEMANTIC)

    def test_parseable_passing_report_is_not_semantic(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.AGENT,
                                  report=rp.ReportOutcome(parsed=True, failed=False))
        result = rp.classify(signal)
        self.assertNotEqual(result.retry_class, st.RetryClass.SEMANTIC)

    def test_failed_post_gate_is_semantic(self):
        signal = rp.FailureSignal(
            node_kind=st.NodeKind.AGENT,
            gate=rp.GateOutcome(pre_gate_failed=True, post_gate_passed=False))
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.SEMANTIC)

    def test_green_pre_gate_blocks_gate_not_falsifiable(self):
        """§7.4 — re-running an agent cannot make a gate falsifiable, so this
        fits no retry class at all (§7.5, §7.3)."""
        signal = rp.FailureSignal(
            node_kind=st.NodeKind.AGENT,
            gate=rp.GateOutcome(pre_gate_failed=False, post_gate_passed=True))
        result = rp.classify(signal)
        self.assertEqual(result.block_reason, st.BlockReason.GATE_NOT_FALSIFIABLE)
        self.assertIsNone(result.retry_class)
        self.assertIn(result.block_reason, st.NON_RETRYABLE)

    def test_code_node_no_effect(self):
        """§7.3 — exit zero, empty diff, expects_changes True: not ENVIRONMENTAL,
        not SEMANTIC, and not silently VERIFIED. Dedicated, non-retryable."""
        signal = rp.FailureSignal(
            node_kind=st.NodeKind.CODE,
            code_effect=rp.CodeEffect(exit_zero=True, diff_empty=True, expects_changes=True))
        result = rp.classify(signal)
        self.assertEqual(result.block_reason, st.BlockReason.CODE_NODE_NO_EFFECT)
        self.assertIn(result.block_reason, st.NON_RETRYABLE)

    def test_idempotent_node_with_default_expectation_is_not_no_effect(self):
        """§7.3 — expects_changes defaults false; an empty diff there is the
        assertive node's normal result, not a violation."""
        signal = rp.FailureSignal(
            node_kind=st.NodeKind.CODE,
            code_effect=rp.CodeEffect(exit_zero=True, diff_empty=True, expects_changes=False))
        result = rp.classify(signal)
        self.assertNotEqual(result.block_reason, st.BlockReason.CODE_NODE_NO_EFFECT)

    def test_launcher_transient_from_pane_allocation(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.AGENT,
                                  launcher_failure=rp.LauncherFailure.PANE_ALLOCATION)
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.LAUNCHER_TRANSIENT)

    def test_unresolved_binary_is_launcher_transient(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.CODE, binary_resolved=False)
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.LAUNCHER_TRANSIENT)

    def test_process_never_started_is_launcher_transient(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.AGENT, process_started=False)
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.LAUNCHER_TRANSIENT)

    def test_unrecognized_shape_defaults_environmental(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.CODE)
        result = rp.classify(signal)
        self.assertIs(result.retry_class, st.DEFAULT_RETRY_CLASS)

    def test_classification_is_exactly_one_of_retry_class_or_block_reason(self):
        with self.assertRaises(ValueError):
            rp.Classification(retry_class=st.RetryClass.SEMANTIC,
                              block_reason=st.BlockReason.GATE_NOT_FALSIFIABLE)
        with self.assertRaises(ValueError):
            rp.Classification()


# ── §7.5 the clause-4 asymmetry — the subtlest rule ──────────────────────────

class ClauseFourAsymmetryTests(unittest.TestCase):
    """A code node's undeclared write blocks PERMISSION_SCOPE_VIOLATION with
    zero retries. An agent node's undeclared write classifies SEMANTIC, never
    ENVIRONMENTAL — because an agent is not deterministic and a retry prompt
    naming the offending paths is genuinely new instructions (§7.5)."""

    def failing_verdict(self):
        return wt.PermissionVerdict(passes=False, conjunct1_violations=("scratch/junk",))

    def test_code_node_clause_four_failure_is_permission_scope_violation(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.CODE, permission=self.failing_verdict())
        result = rp.classify(signal)
        self.assertEqual(result.block_reason, st.BlockReason.PERMISSION_SCOPE_VIOLATION)
        self.assertIsNone(result.retry_class)
        self.assertIn(result.block_reason, st.NON_RETRYABLE)

    def test_agent_node_clause_four_failure_is_semantic(self):
        signal = rp.FailureSignal(node_kind=st.NodeKind.AGENT, permission=self.failing_verdict())
        result = rp.classify(signal)
        self.assertEqual(result.retry_class, st.RetryClass.SEMANTIC)
        self.assertIsNone(result.block_reason)
        self.assertTrue(st.mutates_prompt(result.retry_class))

    def test_passing_verdict_triggers_neither(self):
        passing = wt.PermissionVerdict(passes=True)
        for kind in (st.NodeKind.AGENT, st.NodeKind.CODE):
            signal = rp.FailureSignal(node_kind=kind, permission=passing)
            result = rp.classify(signal)
            self.assertNotEqual(result.block_reason, st.BlockReason.PERMISSION_SCOPE_VIOLATION)
            self.assertNotEqual(result.retry_class, st.RetryClass.SEMANTIC)


# ── §7.5 containment: an unclassified failure is still classified ───────────

class ContainmentTests(unittest.TestCase):

    def test_unseen_exception_type_defaults_environmental(self):
        NeverSeenBefore = type("NeverSeenBefore", (Exception,), {})

        def build_signal():
            raise NeverSeenBefore("an engine bug, not a fact about the code under test")

        result = rp.classify_with_containment(build_signal)
        self.assertIs(result.retry_class, st.DEFAULT_RETRY_CLASS)
        self.assertIsNone(result.block_reason)

    def test_never_semantic_on_containment(self):
        def build_signal():
            raise RuntimeError("boom")

        result = rp.classify_with_containment(build_signal)
        self.assertNotEqual(result.retry_class, st.RetryClass.SEMANTIC)

    def test_healthy_signal_passes_through_unharmed(self):
        def build_signal():
            return rp.FailureSignal(node_kind=st.NodeKind.AGENT,
                                    report=rp.ReportOutcome(parsed=True, failed=True))

        result = rp.classify_with_containment(build_signal)
        self.assertEqual(result.retry_class, st.RetryClass.SEMANTIC)


# ── §7.5 no infra fault decrements the semantic budget ──────────────────────

class NoInfraBudgetDecrementTests(unittest.TestCase):

    def test_environmental_and_launcher_attempts_are_never_counted_semantic(self):
        attempts = [
            make_attempt("n1", "b1", 1, st.RetryClass.ENVIRONMENTAL),
            make_attempt("n1", "b1", 2, st.RetryClass.LAUNCHER_TRANSIENT),
            make_attempt("n1", "b2", 3, st.RetryClass.ENVIRONMENTAL),
        ]
        self.assertEqual(rp.semantic_attempts_total(attempts, "n1"), 0)
        # Three infra failures, and the ceiling still admits the next
        # attempt as if none had happened: no infra fault decrements the
        # semantic budget (§7.5). At a ceiling of 2 the in-flight attempt is
        # the only one counted.
        cfg = make_cfg(semantic_ceiling=2)
        self.assertFalse(ceiling_reached(cfg, "n1", attempts))

    def test_only_semantic_attempts_count_toward_the_ceiling(self):
        attempts = [
            make_attempt("n1", "b1", 1, st.RetryClass.ENVIRONMENTAL),
            make_attempt("n1", "b1", 2, st.RetryClass.SEMANTIC),
            make_attempt("n1", "b2", 3, st.RetryClass.LAUNCHER_TRANSIENT),
            make_attempt("n1", "b2", 4, st.RetryClass.SEMANTIC),
        ]
        self.assertEqual(rp.semantic_attempts_total(attempts, "n1"), 2)


# ── §7.5 the semantic budget: both halves ────────────────────────────────────

class SemanticBudgetTests(unittest.TestCase):

    def test_prompt_mutation_scope_is_per_node_and_base(self):
        """(node_id, base_sha) re-arms with no counter to clear: it is a
        COUNT(*) over the stored rows, derived from a stored fact."""
        attempts = [
            make_attempt("n1", "b1", 1, st.RetryClass.SEMANTIC),
            make_attempt("n1", "b2", 2, st.RetryClass.SEMANTIC),
            make_attempt("n1", "b2", 3, st.RetryClass.SEMANTIC),
        ]
        self.assertEqual(rp.semantic_attempts_at_base(attempts, "n1", "b1"), 1)
        self.assertEqual(rp.semantic_attempts_at_base(attempts, "n1", "b2"), 2)
        self.assertEqual(rp.semantic_attempts_at_base(attempts, "n1", "b3"), 0)

    def test_the_ceiling_stops_the_refund_loop(self):
        """Without the cumulative ceiling, every unrelated merge mints a new
        base_sha and re-arms the per-base scope, so total spend scales with
        the number of merges rather than with the node. Prove the ceiling
        stops it (§7.5)."""
        cfg = make_cfg(semantic_ceiling=3)
        attempts = []
        for i in range(10):
            base = f"base{i}"
            attempts.append(make_attempt("n1", base, i, st.RetryClass.SEMANTIC))
            # per-base scope alone always reports exactly 1 failure — looks
            # "re-armed" on every single merge, forever.
            self.assertEqual(rp.semantic_attempts_at_base(attempts, "n1", base), 1)

        self.assertEqual(rp.semantic_attempts_total(attempts, "n1"), 10)
        self.assertTrue(ceiling_reached(cfg, "n1", attempts))

        # the cumulative ceiling stops the node well before the 10th merge —
        # exactly at K, not scaling with the number of unrelated merges. The
        # enforcer counts the attempt failing right now, so K-1 stored rows
        # plus this one is where it stops.
        first_exhausted_after = next(
            i for i in range(0, 11)
            if ceiling_reached(cfg, "n1", attempts[:i]))
        self.assertEqual(first_exhausted_after + 1, cfg.semantic_ceiling)

    def test_retry_force_grants_exactly_one_attempt_beyond_the_ceiling(self):
        """`maestro retry --force` grants exactly one attempt beyond K without
        raising the cap, read from NodeLifecycle.granted_extra_attempts — the
        lifecycle column, never the audit tier (§5.3, §7.5)."""
        cfg = make_cfg(semantic_ceiling=2)
        attempts = [make_attempt("n1", "b0", 0, st.RetryClass.SEMANTIC)]
        lifecycle = st.NodeLifecycle(node_id="n1")

        # at the ceiling with no grant: exhausted, retry --force is the exit.
        # One stored row plus the attempt failing right now is K=2.
        self.assertTrue(ceiling_reached(
            cfg, "n1", attempts, lifecycle.granted_extra_attempts))

        # one grant admits exactly one more attempt
        lifecycle.granted_extra_attempts = 1
        self.assertFalse(ceiling_reached(
            cfg, "n1", attempts, lifecycle.granted_extra_attempts))

        # spend the granted attempt — it fails semantically too
        attempts.append(make_attempt("n1", "b1", 1, st.RetryClass.SEMANTIC))
        self.assertTrue(ceiling_reached(
            cfg, "n1", attempts, lifecycle.granted_extra_attempts))

        # refuses again without a second grant
        self.assertTrue(ceiling_reached(
            cfg, "n1", attempts, lifecycle.granted_extra_attempts))

        # a second grant admits exactly one more, same pattern
        lifecycle.granted_extra_attempts = 2
        self.assertFalse(ceiling_reached(
            cfg, "n1", attempts, lifecycle.granted_extra_attempts))


# ── §7.5 git results: only the documented not-found exit code is a fact ─────

class GitClassificationTests(unittest.TestCase):

    def test_zero_exit_is_present(self):
        self.assertEqual(rp.classify_git_exit(0, not_found_exit_code=1), rp.GitResult.PRESENT)

    def test_documented_not_found_code_is_absent(self):
        self.assertEqual(rp.classify_git_exit(1, not_found_exit_code=1), rp.GitResult.ABSENT)

    def test_every_other_nonzero_exit_is_environmental_never_absent(self):
        """A transient git failure must never be recorded as a missing
        object — the same principle as 'no report can ever be semantic',
        applied to git (§7.5)."""
        for exit_code in (128, 2, 255):
            self.assertEqual(rp.classify_git_exit(exit_code, not_found_exit_code=1),
                             rp.GitResult.ENVIRONMENTAL_FAILURE)


# ── §7.5 AST detector #1: no comparison against process output text ─────────

class OutputComparisonDetectorTests(unittest.TestCase):
    """A regex over stderr is how ecosystem specifics leak into a general
    engine (§7.5). This detector parses retry_policy.py's own source and
    fails if the classifier compares against stdout/stderr/output content."""

    def test_the_real_module_is_clean(self):
        source = Path(rp.__file__).read_text()
        violations = rp.find_output_content_comparisons(source)
        self.assertEqual(violations, [],
                         f"retry_policy.py compares against process output text: {violations}")

    def test_detector_catches_a_planted_violation(self):
        """A detector never proven to go red on a real violation is not a
        detector — this is that proof."""
        violations = rp.find_output_content_comparisons(rp.PLANTED_OUTPUT_COMPARISON_FIXTURE)
        self.assertGreaterEqual(len(violations), 2)

    def test_detector_ignores_unrelated_comparisons(self):
        clean_source = '''
def classify(exit_code):
    if exit_code == 0:
        return "PRESENT"
    return "ENVIRONMENTAL"
'''
        self.assertEqual(rp.find_output_content_comparisons(clean_source), [])


# ── §7.5 AST detector #2: no unclassified git failure is a repository fact ──

class GitAbsenceDetectorTests(unittest.TestCase):

    def test_the_real_module_is_clean(self):
        source = Path(rp.__file__).read_text()
        violations = rp.find_ungated_git_absence(source)
        self.assertEqual(violations, [],
                         f"retry_policy.py concludes git absence without an "
                         f"equality-gated not-found check: {violations}")

    def test_detector_catches_a_planted_violation(self):
        violations = rp.find_ungated_git_absence(rp.PLANTED_GIT_ABSENCE_FIXTURE)
        self.assertGreaterEqual(len(violations), 1)

    def test_detector_accepts_the_equality_gated_form(self):
        clean_source = '''
def check(exit_code, not_found_exit_code):
    if exit_code == not_found_exit_code:
        return "ABSENT"
    return "ENVIRONMENTAL_FAILURE"
'''
        self.assertEqual(rp.find_ungated_git_absence(clean_source), [])


# ── §7.5 launcher budgets, credential = 0 ────────────────────────────────────

class LauncherBudgetTests(unittest.TestCase):

    def test_credential_failure_has_zero_retry_budget(self):
        cfg = make_cfg()
        self.assertEqual(cfg.credential_retries, 0)
        self.assertEqual(rp.launcher_retry_budget(cfg, rp.LauncherFailure.CREDENTIAL), 0)

    def test_other_launcher_failures_use_the_launcher_budget(self):
        cfg = make_cfg()
        for failure in (rp.LauncherFailure.PANE_ALLOCATION, rp.LauncherFailure.STARTUP,
                        rp.LauncherFailure.TRANSPORT):
            self.assertEqual(rp.launcher_retry_budget(cfg, failure), cfg.launcher_retries)


if __name__ == "__main__":
    unittest.main()
