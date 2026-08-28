"""A repair launch must re-point the attempt row at the pane it opened.

`attempts.extra` is written once per attempt, by `store.mark_launched` on the
first-attempt path in `run_node`. A repair round opens a **new** pane inside
that same attempt, and neither replacement launcher marked it:

  * `continue_tests_node` -- the tests-node repair, added with §19 M41's
    repair half.
  * `launch_replacement` -- the builder repair, pre-existing. The tests path
    matched its sibling's behaviour rather than diverging from it, so this is
    one defect at two call sites.

The row therefore kept naming the *rejected* actor's transcript, pid, and
process start epoch for the rest of the attempt. It is silent: the stale path
still resolves and a transcript is still there, so a post-hoc read of a lane
that was rejected, repaired, and merged -- exactly the round anyone goes back
to read -- reads the wrong pane and reports it as that lane's evidence.
§1.1 item 4 requires a merged node's evidence chain to be complete, and a
chain whose transcript names a different actor is not.

Every case here drives the **real** `continue_node` closure captured out of
`maestro._execute_run` by `test_tests_node_repair.captured_continue_node`.
The pre-existing repair tests all inject `deps(continue_node=...)` fakes,
which is how a missing store write on both production launchers went
unnoticed.

  P1  a tests-node repair leaves `attempts.extra` naming its own tester
  P2  a builder replacement leaves it naming the replacement builder
  P3  each records the identity of the actor its own lane kind dispatched
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import attempt_identity  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402
from test_tests_node_repair import captured_continue_node  # noqa: E402


FINDINGS = "Assert complete expected pairs, and an observable resolver result"
REJECTED = "a" * 40
#: What the first launch of this attempt wrote. Distinct from anything the
#: recording runner hands a repair back, so a row that never moves is
#: distinguishable from one that moved to the right pane.
FIRST_ACTOR_TRANSCRIPT = "/first-actor/transcript.jsonl"


class _ExtraLedger:
    """`attempts.extra` with the real merge semantics, seeded as launched.

    `LifecycleStore.mark_launched` reads the row's `extra_json`, `update`s the
    mapping it was handed onto it, and writes it back. Asserting against a
    bare mock call would prove a call happened; asserting against this proves
    what the row *says* afterwards, which is what a post-hoc reader gets.
    """

    def __init__(self, store):
        self.extra = {
            wd.SESSION_PATH_KEY: FIRST_ACTOR_TRANSCRIPT,
            attempt_identity.VENDOR_KEY: "first-actor-vendor",
            attempt_identity.MODEL_KEY: "first-actor-model",
            attempt_identity.ROUTE_KEY: "first-actor-route",
        }
        self.pid = 4242
        store.mark_launched.side_effect = self._mark_launched

    def _mark_launched(
        self, _run_id, _node_id, _attempt_no, pid, launched_at=None, extra=None
    ):
        if extra:
            self.extra.update(extra)
        self.pid = pid

    @property
    def session_path(self):
        return self.extra[wd.SESSION_PATH_KEY]


def _repair(harness):
    return harness.continue_node(
        harness.attempt,
        harness.node,
        harness.record,
        "Repair rejected candidate.\n\n{}".format(FINDINGS),
        REJECTED,
        1,
        lambda: False,
    )


class TestsRepairRebindsTheAttemptTranscript(unittest.TestCase):
    """P1 — the tests path, added with the repair half of §19 M41."""

    def test_the_row_names_the_repair_tester_not_the_rejected_one(self):
        with captured_continue_node() as harness:
            ledger = _ExtraLedger(harness.store)
            _repair(harness)
            launched = harness.runner.launched[0]
            transcript = str(harness.runner.root / "tester.jsonl")
        self.assertNotEqual(FIRST_ACTOR_TRANSCRIPT, transcript)
        self.assertEqual(ledger.session_path, transcript)
        # The pane that was actually opened, not merely some pane: the
        # launched spec and the marked row agree on one turn.
        self.assertIn("tester", launched.correlation_token)

    def test_the_row_names_the_actor_the_tests_lane_dispatched(self):
        """P3 — a tests lane repairs on the tester's vendor, not the build's."""
        with captured_continue_node() as harness:
            ledger = _ExtraLedger(harness.store)
            _repair(harness)
        self.assertEqual(ledger.extra[attempt_identity.VENDOR_KEY], "test-vendor")
        self.assertEqual(ledger.extra[attempt_identity.MODEL_KEY], "test-model")
        self.assertEqual(ledger.extra[attempt_identity.ROUTE_KEY], "claude")

    def test_a_second_round_rebinds_again(self):
        """Repair rounds share one attempt, so the row must move each time."""
        with captured_continue_node() as harness:
            ledger = _ExtraLedger(harness.store)
            _repair(harness)
            harness.store.mark_launched.reset_mock()
            harness.continue_node(
                harness.attempt,
                harness.node,
                harness.record,
                FINDINGS,
                "b" * 40,
                1,
                lambda: False,
            )
            calls = harness.store.mark_launched.call_args_list
            transcript = str(harness.runner.root / "tester.jsonl")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ledger.session_path, transcript)


def _builder_harness(harness):
    """Drive the AGENT branch down `launch_replacement`.

    The retained builder session cannot be adopted, which is the only route
    into the replacement launcher. Its generation must survive into the
    recovered session or `continue_node` raises before the launch stands.
    """
    session = SimpleNamespace(
        generation=1,
        correlation_token="run-1-lane-wp6-builder-g1",
        pane_id="w1:p1",
        session_path=FIRST_ACTOR_TRANSCRIPT,
        tab_id="w1:t1",
        state=st.ActorSessionState.ACTIVE,
    )
    replacement_token = "run-1-lane-wp6-builder-g2"
    harness.store.current_actor_session.return_value = session
    harness.store.actor_sessions.return_value = [session]
    # The replacement is generation 2; the fixture's handoff answers for the
    # generation the rejected turn ran under.
    harness.store.mark_handoff_submitted.return_value = SimpleNamespace(
        submitted=True, handoff=SimpleNamespace(builder_generation=2)
    )
    harness.store.recover_builder_handoff.return_value = SimpleNamespace(
        recovered=True,
        session=SimpleNamespace(
            generation=2,
            correlation_token=replacement_token,
            pane_id="w1:p2",
            session_path=str(harness.runner.root / "tester.jsonl"),
        ),
    )

    def absent(_persisted):
        raise lch.HandleAbsent("no pane")

    harness.runner.adopt = absent
    return replacement_token


class BuilderReplacementRebindsTheAttemptTranscript(unittest.TestCase):
    """P2 — the pre-existing builder path, unmarked since it was written."""

    def test_the_row_names_the_replacement_builder(self):
        with captured_continue_node(kind=st.NodeKind.AGENT) as harness:
            ledger = _ExtraLedger(harness.store)
            token = _builder_harness(harness)
            _repair(harness)
            launched = harness.runner.launched[0]
            transcript = str(harness.runner.root / "tester.jsonl")
        self.assertEqual(launched.correlation_token, token)
        self.assertNotEqual(FIRST_ACTOR_TRANSCRIPT, transcript)
        self.assertEqual(ledger.session_path, transcript)

    def test_the_row_names_the_actor_the_build_lane_dispatched(self):
        """P3 — the build lane's own vendor, resolved from the node's kind."""
        with captured_continue_node(kind=st.NodeKind.AGENT) as harness:
            ledger = _ExtraLedger(harness.store)
            _builder_harness(harness)
            _repair(harness)
        self.assertEqual(ledger.extra[attempt_identity.VENDOR_KEY], "build-vendor")
        self.assertEqual(ledger.extra[attempt_identity.MODEL_KEY], "build-model")
        self.assertEqual(ledger.extra[attempt_identity.ROUTE_KEY], "omp")


if __name__ == "__main__":
    unittest.main()
