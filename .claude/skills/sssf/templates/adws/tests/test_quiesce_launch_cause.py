"""§16.3 item 45 — a failed launch keeps its own cause through the quiesce.

`Scheduler._attempt_body` must prove an attempt's owned execution absent before
anything classifies or releases it (§8.3), and for as long as that proof lived
in a bare `finally` it destroyed the very failure it was guarding. Python
replaces an in-flight exception with one raised inside a `finally`, so a launch
that failed before its handle was registered blocked `QUIESCENCE_UNPROVEN` —
terminal — with the launcher's own class gone, about a process that was never
started. Observed on a node refused `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING`.

The repair the item prescribes, and what each half of it is worth:

* The launcher reports, **as a typed field on the refusal**, whether a pane was
  created before it failed. Not inferred from the message, which would be the
  lexical shortcut §7.5 forbids, and not assumed from the fact of failure,
  which would lie.
* Quiesce is skipped **only** where that field says nothing was created —
  absence by construction rather than absence asserted. Marking every failed
  launch proven-absent would report a fact nobody measured for the refusals
  raised after the pane split (`SHELL_NOT_READY`, `NO_PANE`), where a pane
  really does exist and its group is exactly what quiescence is for.

Both arms are driven below. Neither passes under the other's implementation:
the pre-pane arm fails if quiesce still runs, and the post-pane arm fails if
the skip is widened to every launch failure.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import launcher as lch  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_scheduler import SchedulerFixture  # noqa: E402


def wrapped(
    refusal: lch.LaunchRefusal,
    detail: str = "",
    failure: rp.LauncherFailure = rp.LauncherFailure.STARTUP,
) -> sch.LaunchFailed:
    """A refusal as the runtime delivers it to the scheduler.

    `maestro._typed_launch` catches whatever the launcher raised and re-raises
    `scheduler.LaunchFailed(...) from exc`, so the typed refusal arrives as the
    wrapper's `__cause__`. Reproduced exactly here rather than constructed by
    hand, because the chaining *is* the mechanism under test: a `LaunchFailed`
    built without `from` carries no refusal and must fall back to quiescing.
    """
    try:
        raise lch.LaunchRefused(refusal, detail)
    except lch.LaunchRefused as exc:
        try:
            raise sch.LaunchFailed(
                failure, "{0}: {1}".format(type(exc).__name__, exc)
            ) from exc
        except sch.LaunchFailed as wrapper:
            return wrapper


# ── the launcher's own typed report ─────────────────────────────────────────


class RefusalFactsTests(unittest.TestCase):
    def test_a_pre_split_refusal_reports_that_no_pane_exists(self):
        """`pane_env_flags` runs while the split's arguments are being built,
        so its refusal is raised before herdr is called at all."""
        with self.assertRaises(lch.LaunchRefused) as caught:
            lch.pane_env_flags({})
        self.assertIs(
            caught.exception.refusal, lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING
        )
        self.assertFalse(caught.exception.pane_created)

    def test_post_split_refusals_report_that_a_pane_may_exist(self):
        """§16.3 item 45 names both by name: marking these proven-absent would
        report a fact nobody measured."""
        for refusal in (lch.LaunchRefusal.SHELL_NOT_READY, lch.LaunchRefusal.NO_PANE):
            with self.subTest(refusal=refusal.code):
                self.assertTrue(lch.LaunchRefused(refusal).pane_created)

    def test_the_message_is_unchanged_so_the_ledger_reads_the_same(self):
        self.assertEqual(
            str(
                lch.LaunchRefused(
                    lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING, "TMPDIR,PYTEST_ADDOPTS"
                )
            ),
            "LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:TMPDIR,PYTEST_ADDOPTS",
        )
        self.assertEqual(
            str(lch.LaunchRefused(lch.LaunchRefusal.NO_PANE)), "LAUNCH_REFUSED:NO_PANE"
        )

    def test_an_untyped_launch_failure_still_demands_the_proof(self):
        """Fail-closed. A `LaunchFailed` that carries no refusal has not
        stated that it created nothing, and §8.3 refuses to assume it."""
        self.assertTrue(sch.LaunchFailed(rp.LauncherFailure.TRANSPORT).pane_created)


# ── the scheduler, both arms ────────────────────────────────────────────────


class LaunchCauseSurvivesQuiesceTests(SchedulerFixture):
    def _quiesce_phases(self, node_id="a"):
        return [
            phase for (run, node, _), phase in self.quiesce_calls if node == node_id
        ]

    def test_a_pre_pane_refusal_skips_the_proof_and_keeps_its_class(self):
        """The observed incident, driven end to end.

        With the proof skipped, the refusal reaches the containment handler,
        `classify` sees a typed launcher member, and the node blocks for the
        refusal. Under the old `finally` this node blocked
        QUIESCENCE_UNPROVEN on attempt 1 and the launcher class was gone.
        """
        self.raise_for = {
            "a": wrapped(
                lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING, "PYTHONPYCACHEPREFIX"
            )
        }
        self.schedule([self.agent("a")]).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.state, st.NodeState.BLOCKED)
        self.assertIsNot(record.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertNotIn("pre-inventory", self._quiesce_phases())

    def test_the_incident_itself_a_raising_proof_over_a_pre_pane_refusal(self):
        """The observed production shape, not a weakened version of it.

        `maestro`'s quiesce resolves the attempt's handle from a map the
        runner populates only after a *successful* launch, so a refusal raised
        before registration finds no key and raises PROCESS_GROUP_UNTRACKED —
        modelled exactly here. Under the old bare `finally` that second
        exception replaced the first: the node blocked QUIESCENCE_UNPROVEN,
        terminally, about a process nothing ever started, and the refusal's
        own class never reached the classifier. A fixture whose quiesce always
        succeeds cannot see the difference, which is why this case exists
        beside the call-count one above.
        """
        self.raise_for = {
            "a": wrapped(lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING, "PYTEST_ADDOPTS")
        }
        original = self.quiesce_attempt

        def untracked(record, phase):
            original(record, phase)
            if phase == "pre-inventory":
                raise RuntimeError(
                    "PROCESS_GROUP_UNTRACKED:pre-inventory:{0}#{1}".format(
                        record.node_id, record.attempt_no
                    )
                )

        self.schedule(
            [self.agent("a")], deps=self.deps(quiesce_attempt=untracked)
        ).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.block_reason, st.BlockReason.LAUNCH_REFUSED)
        self.assertIsNot(record.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)

    def test_the_refusal_reaches_the_ledger_rather_than_a_quiescence_error(self):
        self.raise_for = {
            "a": wrapped(lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING, "TMPDIR")
        }
        self.schedule([self.agent("a")]).run()

        blocked = [
            t
            for t in self.store.audit_transitions("run1")
            if t.get("node_id") == "a"
            and t.get("to_state") == st.NodeState.BLOCKED.value
        ]
        self.assertTrue(blocked)
        self.assertIn("SCRATCH_REDIRECT_MISSING", str(blocked[-1]))

    def test_a_post_pane_refusal_still_requires_a_measured_absence(self):
        """The half the naive repair gets wrong. A pane may exist, so the
        proof runs exactly as it did before — and when the harness cannot
        supply it, QUIESCENCE_UNPROVEN is the correct answer."""
        self.raise_for = {"a": wrapped(lch.LaunchRefusal.SHELL_NOT_READY)}
        self.schedule([self.agent("a")]).run()

        self.assertIn("pre-inventory", self._quiesce_phases())

    def test_a_failing_proof_over_a_post_pane_refusal_keeps_both_causes(self):
        """When quiesce genuinely fails while a launch exception is in flight,
        the node blocks on the proof — and the launch's own cause is not
        silently dropped: it is the chained context the block detail records.
        """
        launch = wrapped(lch.LaunchRefusal.SHELL_NOT_READY)
        self.raise_for = {"a": launch}
        original = self.quiesce_attempt

        def failing(record, phase):
            original(record, phase)
            if phase == "pre-inventory":
                raise RuntimeError("PROCESS_GROUP_UNTRACKED:pre-inventory:a#1")

        self.schedule([self.agent("a")], deps=self.deps(quiesce_attempt=failing)).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        blocked = [
            t
            for t in self.store.audit_transitions("run1")
            if t.get("node_id") == "a"
            and t.get("to_state") == st.NodeState.BLOCKED.value
        ]
        detail = str(blocked[-1])
        self.assertIn("PROCESS_GROUP_UNTRACKED", detail)
        self.assertIn("SHELL_NOT_READY", detail)

    def test_an_ordinary_runner_failure_is_quiesced_exactly_as_before(self):
        """The skip is scoped to a typed refusal. Nothing else changes."""
        self.raise_for = {"a": RuntimeError("boom")}
        self.schedule([self.agent("a")]).run()
        self.assertIn("pre-inventory", self._quiesce_phases())

    def test_a_successful_attempt_is_quiesced_at_candidate_idle(self):
        """§8.3 closes the successful launch bracket before candidate
        measurement; `candidate-idle` is not skippable."""
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")]).run()
        self.assertIn("candidate-idle", self._quiesce_phases())


if __name__ == "__main__":
    unittest.main()
