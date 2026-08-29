"""§16.3 item 46 — a deterministic launch refusal blocks on its first attempt.

LAUNCHER_TRANSIENT is named for an assumption: that another attempt might
survive what this one did not. A refusal that is deterministic by construction
satisfies every structural test for the class and violates that assumption. A
call site that omits an environment omits it identically on every attempt, so
the node made two more launches that could not succeed and then blocked
`LAUNCHER_BUDGET_EXHAUSTED` — a reason that says a budget ran out when nothing
was ever retryable.

**The ruling this file executes, and why it is not a fourth retry class.**
§7.5 closes the retry classes at three and makes the closure load-bearing, so
widening the table is a design change and is ruled out. It does not close the
*members* inside a class, and it says so explicitly: "the budget is a property
of the member, not of the class", which is why `LauncherFailure.CREDENTIAL`
already carries zero inside a class named for transient faults. A deterministic
refusal is the second member of that same shape. Three classes, one more
member, `launcher_retry_budget` reading the partition — which is exactly the
discharge item 46 asks for, and the narrower of the two shapes it offers.

The determinism travels as a typed field on the launcher's own refusal.
Matching `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:` to reach the same
conclusion would be the lexical shortcut §7.5 forbids and `test_no_dead_seams`'
sibling AST guard convicts; the negative test for that is in
`test_launcher_classification.py`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import launcher as lch          # noqa: E402
from adw_modules import retry_policy as rp       # noqa: E402
from adw_modules import scheduler as sch         # noqa: E402
from adw_modules import scheduler_types as st    # noqa: E402

from test_quiesce_launch_cause import wrapped    # noqa: E402
from test_scheduler import SchedulerFixture      # noqa: E402


def make_cfg(**overrides):
    defaults = dict(concurrency=1, node_timeout_s=60.0, turn_timeout_s=30.0,
                    final_acceptance_timeout_s=60.0, backstop_t_s=600.0,
                    semantic_ceiling=3)
    defaults.update(overrides)
    return st.SchedulerConfig(**defaults)


# ── the partition and the budget that reads it ──────────────────────────────

class DeterministicPartitionTests(unittest.TestCase):

    def test_the_partition_is_over_members_not_over_classes(self):
        """§7.5's three classes are untouched: the vocabulary this widens is
        LAUNCHER_TRANSIENT's membership, which the section leaves open."""
        self.assertEqual({c.value for c in st.RetryClass},
                         {"SEMANTIC", "ENVIRONMENTAL", "LAUNCHER_TRANSIENT"})
        self.assertIn(rp.LauncherFailure.DETERMINISTIC_REFUSAL,
                      rp.DETERMINISTIC_LAUNCHER_FAILURES)
        self.assertIn(rp.LauncherFailure.CREDENTIAL,
                      rp.DETERMINISTIC_LAUNCHER_FAILURES)

    def test_a_deterministic_refusal_has_no_budget_to_spend(self):
        cfg = make_cfg()
        self.assertEqual(
            rp.launcher_retry_budget(cfg, rp.LauncherFailure.DETERMINISTIC_REFUSAL),
            0)

    def test_the_transient_members_keep_their_budget(self):
        """The narrowing is exact. A dropped transport is still retried."""
        cfg = make_cfg()
        for member in (rp.LauncherFailure.PANE_ALLOCATION,
                       rp.LauncherFailure.STARTUP,
                       rp.LauncherFailure.TRANSPORT):
            with self.subTest(member=member.value):
                self.assertEqual(rp.launcher_retry_budget(cfg, member),
                                 cfg.launcher_retries)
                self.assertGreater(cfg.launcher_retries, 0)

    def test_the_block_names_the_refusal_rather_than_the_budget(self):
        self.assertIs(
            sch._budget_reason(st.RetryClass.LAUNCHER_TRANSIENT,
                               rp.LauncherFailure.DETERMINISTIC_REFUSAL),
            st.BlockReason.LAUNCH_REFUSED)
        self.assertIs(
            sch._budget_reason(st.RetryClass.LAUNCHER_TRANSIENT,
                               rp.LauncherFailure.TRANSPORT),
            st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)

    def test_classification_is_still_launcher_transient(self):
        """The class does not change; only the budget the member carries."""
        result = rp.classify(rp.FailureSignal(
            node_kind=st.NodeKind.AGENT,
            launcher_failure=rp.LauncherFailure.DETERMINISTIC_REFUSAL))
        self.assertIs(result.retry_class, st.RetryClass.LAUNCHER_TRANSIENT)
        self.assertIs(result.launcher_failure,
                      rp.LauncherFailure.DETERMINISTIC_REFUSAL)


# ── the upgrade, read from the refusal's type and never from its message ────

class ClassifiedFailureTests(unittest.TestCase):

    def test_a_deterministic_refusal_is_upgraded_to_the_zero_budget_member(self):
        """The adapter's own `classify` maps CONFIGURATION to STARTUP, which
        carries a budget. The launcher's typed field is what overrules it."""
        exc = wrapped(lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING, "TMPDIR")
        self.assertIs(exc.failure, rp.LauncherFailure.STARTUP)
        self.assertIs(exc.classified_failure,
                      rp.LauncherFailure.DETERMINISTIC_REFUSAL)

    def test_a_transient_refusal_keeps_the_member_the_adapter_chose(self):
        exc = wrapped(lch.LaunchRefusal.SHELL_NOT_READY,
                      failure=rp.LauncherFailure.PANE_ALLOCATION)
        self.assertIs(exc.classified_failure, rp.LauncherFailure.PANE_ALLOCATION)

    def test_an_unchained_failure_is_never_upgraded(self):
        """No refusal, no claim. A bare `LaunchFailed` keeps its member."""
        self.assertIs(
            sch.LaunchFailed(rp.LauncherFailure.STARTUP).classified_failure,
            rp.LauncherFailure.STARTUP)


# ── the scheduler: one attempt, then a block that says what was refused ─────

class DeterministicRefusalEndToEndTests(SchedulerFixture):

    def _attempt_count(self, node_id="a"):
        return len(self.store.attempts_for("run1", node_id))

    def test_a_deterministic_refusal_blocks_on_its_first_occurrence(self):
        original = self.run_node

        def always_refused(attempt, node, record, retry_prompt, on_launch,
                           cancel_requested):
            raise wrapped(lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING,
                          "PYTHONPYCACHEPREFIX")

        self.schedule([self.agent("a")],
                      deps=self.deps(run_node=always_refused)).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.state, st.NodeState.BLOCKED)
        self.assertIs(record.block_reason, st.BlockReason.LAUNCH_REFUSED)
        self.assertEqual(self._attempt_count(), 1)

    def test_a_transient_refusal_still_spends_its_launcher_budget(self):
        """The control. Without it this file would prove only that something
        blocks early, not that the narrowing is confined to the deterministic
        member — and a change that blocked every launcher failure on its first
        attempt would pass the test above."""
        def always_refused(attempt, node, record, retry_prompt, on_launch,
                           cancel_requested):
            raise wrapped(lch.LaunchRefusal.SHELL_NOT_READY,
                          failure=rp.LauncherFailure.PANE_ALLOCATION)

        self.schedule([self.agent("a")],
                      deps=self.deps(run_node=always_refused)).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.block_reason,
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)
        self.assertGreater(self._attempt_count(), 1)


if __name__ == "__main__":
    unittest.main()
