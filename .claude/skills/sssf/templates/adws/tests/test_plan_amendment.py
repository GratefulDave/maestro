"""Executable proof of which plan amendments a run in flight may adopt.

The dead end this exists to remove, from 2026-08-27: `lane-routing-chemical`
was given `min_cases: 5` against an accepted test candidate defining 2 — a gate
no honest attempt could pass. Fixing the plan meant `plan ship` rewriting the
file, which changed its digest, which made `_resume_run_selection` refuse the
run forever: *"the plan file has changed since the run started."* The only
escape was abandoning a run holding **9 MERGED and 4 ACCEPTED** nodes.

That refusal is correct and is not what these tests loosen. Resuming a run
against different plan bytes silently is precisely the substitution it prevents.
What these settle is the narrower question that makes it selective: **given what
the run has already merged, is this particular amendment safe to adopt?**

Node states come from a **real `LifecycleStore`**, driven to MERGED and RUNNING
through the real transitions, rather than from a dict written by hand. That is
deliberate and it is the `CeilingProbe` lesson (§16.3 item 62) applied before
the fact: a fake that returns whatever state a case needs would bless every rule
here, including a wrong one, because the thing under test is precisely the
relationship between a node's state and what may be changed about it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import plan_amendment as pa  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_lifecycle import make_node, new_store  # noqa: E402

RUN_ID = "run-amend"


def _agent(node_id, depth=0, needs=(), min_cases=2, instruction="do the work"):
    """A projected agent node, the way `to_plan_nodes` emits one."""
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.AGENT,
        depth=depth,
        needs=tuple(needs),
        outputs=("src/{0}.py".format(node_id),),
        gate_command=("pytest", "tests/test_{0}.py".format(node_id)),
        gate_selector="tests/test_{0}.py".format(node_id),
        gate_min_cases=min_cases,
        instruction=instruction,
    )


class _Run:
    """A real run whose node states are read back from the real store."""

    def __init__(self, tmp, nodes):
        self.store = new_store(Path(tmp))
        self.plan = tuple(nodes)
        self.store.create_run(
            RUN_ID, "d", [make_node(n.node_id, n.depth) for n in self.plan]
        )

    def merge(self, node_id):
        self.store.start_attempt(RUN_ID, node_id, base_sha="s1")
        self.store.mark_verified(RUN_ID, node_id, output_sha="a" * 40)
        self.store.mark_merged(RUN_ID, node_id)

    def start(self, node_id):
        self.store.start_attempt(RUN_ID, node_id, base_sha="s1")

    def states(self):
        return {
            node.node_id: self.store.get_node(RUN_ID, node.node_id).state
            for node in self.plan
        }

    def classify(self, amended, **kwargs):
        return pa.classify(self.plan, amended, self.states(), **kwargs)


class TheAmendmentTheRunActuallyNeeded(unittest.TestCase):
    """The 2026-08-27 case: fix one blocked lane, keep the merged work."""

    def test_lowering_an_unsatisfiable_gate_on_an_unstarted_lane_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The real shape: the blocked lane demands 5 against tests that
            # define 2, which is the gate no honest attempt could pass.
            run = _Run(
                tmp,
                [
                    _agent("merged-a"),
                    _agent("merged-b"),
                    _agent("blocked", min_cases=5),
                ],
            )
            self.addCleanup(run.store.close)
            run.merge("merged-a")
            run.merge("merged-b")
            self.assertEqual(
                run.states()["merged-a"], st.NodeState.MERGED, "precondition"
            )
            self.assertEqual(run.plan[2].gate_min_cases, 5, "precondition")

            amended = [
                run.plan[0],
                run.plan[1],
                replace(run.plan[2], gate_min_cases=2),
            ]
            verdict = run.classify(amended)

            self.assertTrue(verdict.amendable, verdict.as_mapping())
            self.assertEqual(verdict.changed, ("blocked",))
            self.assertEqual(verdict.removed, ())

    def test_correcting_a_blocked_lanes_instruction_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("merged-a"), _agent("blocked")])
            self.addCleanup(run.store.close)
            run.merge("merged-a")

            amended = [
                run.plan[0],
                replace(run.plan[1], instruction="do the work, correctly stated"),
            ]
            verdict = run.classify(amended)

            self.assertTrue(verdict.amendable, verdict.as_mapping())
            self.assertEqual(verdict.changed, ("blocked",))

    def test_adding_a_node_nothing_existing_depends_on_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("merged-a"), _agent("blocked")])
            self.addCleanup(run.store.close)
            run.merge("merged-a")

            amended = list(run.plan) + [_agent("new-tests", depth=1, needs=("blocked",))]
            verdict = run.classify(amended)

            self.assertTrue(verdict.amendable, verdict.as_mapping())
            self.assertEqual(verdict.added, ("new-tests",))


class MergedWorkIsNotRetconned(unittest.TestCase):
    """§1.1 item 4 — a settled node's evidence was measured against its terms."""

    def _merged_run(self, tmp):
        run = _Run(tmp, [_agent("merged-a"), _agent("pending-b")])
        run.merge("merged-a")
        return run

    def test_changing_a_merged_nodes_gate_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._merged_run(tmp)
            self.addCleanup(run.store.close)
            amended = [replace(run.plan[0], gate_min_cases=99), run.plan[1]]

            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals], [pa.Refusal.SETTLED_NODE_CHANGED]
            )
            self.assertEqual(verdict.refusals[0].node_id, "merged-a")

    def test_swapping_a_merged_nodes_outputs_is_refused_as_an_addition(self):
        """Refused under the sharper code: it would claim an unmade production.

        This used to assert the generic `SETTLED_NODE_CHANGED`. The outputs
        rules are now finer-grained, and a swap trips the *addition* arm first,
        which is the more informative answer — the node never wrote
        `src/elsewhere.py`, and no completed measurement can support it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = self._merged_run(tmp)
            self.addCleanup(run.store.close)
            amended = [replace(run.plan[0], outputs=("src/elsewhere.py",)), run.plan[1]]

            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals], [pa.Refusal.SETTLED_OUTPUT_ADDED]
            )

    def test_changing_a_merged_nodes_instruction_is_refused(self):
        """Even the instruction: it is the contract its review judged."""
        with tempfile.TemporaryDirectory() as tmp:
            run = self._merged_run(tmp)
            self.addCleanup(run.store.close)
            amended = [replace(run.plan[0], instruction="something else"), run.plan[1]]

            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)

    def test_removing_a_merged_node_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._merged_run(tmp)
            self.addCleanup(run.store.close)

            verdict = run.classify([run.plan[1]])

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals], [pa.Refusal.SETTLED_NODE_REMOVED]
            )
            self.assertEqual(verdict.removed, ("merged-a",))

    def test_removing_an_unstarted_node_is_permitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self._merged_run(tmp)
            self.addCleanup(run.store.close)

            verdict = run.classify([run.plan[0]])

            self.assertTrue(verdict.amendable, verdict.as_mapping())
            self.assertEqual(verdict.removed, ("pending-b",))


class AFinishedPathMayBeHandedOn(unittest.TestCase):
    """The narrow exception: a settled *agent* node releasing a spent path.

    A node's `outputs` do two jobs — write permission during the attempt, and
    an ownership claim in the plan. For a MERGED node the first is spent and
    the second is a property of the graph, so releasing a finished path
    re-judges no evidence. The scope is decided by a measurement rather than a
    preference: every production reader of `node.outputs` is attempt-time or
    review-time **except two, and both read a tests node** —
    `compare_test_bytes` pairs every later build lane against
    `tuple(tests_node.outputs)`, and `_append_needed_tests` reads them for a
    dependant's prompt.
    """

    def test_a_merged_agent_node_may_hand_a_finished_path_to_a_live_lane(self):
        """The amendment the EPA run actually needs."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("acquisition"), _agent("blocked")])
            self.addCleanup(run.store.close)
            run.merge("acquisition")

            amended = [
                replace(run.plan[0], outputs=()),
                replace(
                    run.plan[1],
                    outputs=("src/blocked.py", "src/acquisition.py"),
                ),
            ]
            verdict = run.classify(amended)

            self.assertTrue(verdict.amendable, verdict.as_mapping())
            self.assertEqual(
                verdict.transfers,
                (("src/acquisition.py", "acquisition", "blocked"),),
            )

    def test_a_merged_tests_node_may_not_because_its_outputs_are_still_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = replace(_agent("tests-lane"), kind=st.NodeKind.TESTS)
            run = _Run(tmp, [tests, _agent("blocked")])
            self.addCleanup(run.store.close)
            run.merge("tests-lane")

            amended = [
                replace(tests, outputs=()),
                replace(run.plan[1], outputs=("src/blocked.py", "src/tests-lane.py")),
            ]
            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals],
                [pa.Refusal.SETTLED_TESTS_OUTPUT_CHANGED],
            )

    def test_a_released_path_nobody_takes_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("acquisition"), _agent("blocked")])
            self.addCleanup(run.store.close)
            run.merge("acquisition")

            amended = [replace(run.plan[0], outputs=()), run.plan[1]]
            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals],
                [pa.Refusal.TRANSFER_WITHOUT_RECIPIENT],
            )

    def test_a_path_may_not_be_handed_to_another_settled_node(self):
        """A settled recipient cannot write it, so the hand-over is a fiction."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("acquisition"), _agent("other")])
            self.addCleanup(run.store.close)
            run.merge("acquisition")
            run.merge("other")

            amended = [
                replace(run.plan[0], outputs=()),
                replace(run.plan[1], outputs=("src/other.py", "src/acquisition.py")),
            ]
            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)
            self.assertIn(
                pa.Refusal.TRANSFER_WITHOUT_RECIPIENT,
                [f.code for f in verdict.refusals],
            )

    def test_a_transfer_bundled_with_any_other_change_is_not_a_transfer(self):
        """The exception is a *bare* hand-over, not a licence to edit."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("acquisition"), _agent("blocked")])
            self.addCleanup(run.store.close)
            run.merge("acquisition")

            amended = [
                replace(run.plan[0], outputs=(), instruction="and do this too"),
                replace(
                    run.plan[1], outputs=("src/blocked.py", "src/acquisition.py")
                ),
            ]
            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals], [pa.Refusal.SETTLED_NODE_CHANGED]
            )


class ALiveAttemptIsNotAmendedUnderneath(unittest.TestCase):
    def test_changing_a_running_nodes_spec_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("running-a")])
            self.addCleanup(run.store.close)
            run.start("running-a")
            self.assertEqual(run.states()["running-a"], st.NodeState.RUNNING)

            verdict = run.classify([replace(run.plan[0], instruction="new terms")])

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals], [pa.Refusal.RUNNING_NODE_CHANGED]
            )


class TheGraphAmongExistingNodesIsFrozen(unittest.TestCase):
    """§19 M42's shape: rewiring `needs` reopens admitted decisions."""

    def test_changing_an_existing_nodes_needs_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("a"), _agent("b", depth=1, needs=("a",))])
            self.addCleanup(run.store.close)

            amended = [run.plan[0], replace(run.plan[1], needs=())]
            verdict = run.classify(amended)

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals], [pa.Refusal.GRAPH_EDGE_CHANGED]
            )


class TheWholeRunBarIsFrozenOnceAnythingMerges(unittest.TestCase):
    def test_merge_policy_change_is_refused_after_a_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("a")])
            self.addCleanup(run.store.close)
            run.merge("a")

            verdict = run.classify(
                list(run.plan),
                current_merge_policy=("pytest", 10),
                amended_merge_policy=("pytest", 1),
            )

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals], [pa.Refusal.MERGE_POLICY_CHANGED]
            )

    def test_merge_policy_change_is_permitted_before_anything_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("a")])
            self.addCleanup(run.store.close)

            verdict = run.classify(
                list(run.plan),
                current_merge_policy=("pytest", 10),
                amended_merge_policy=("pytest", 1),
            )

            self.assertTrue(verdict.amendable, verdict.as_mapping())

    def test_a_schema_change_is_never_an_amendment(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("a")])
            self.addCleanup(run.store.close)

            verdict = run.classify(
                list(run.plan),
                current_schema="maestro-plan.v4",
                amended_schema="maestro-plan.v5",
            )

            self.assertFalse(verdict.amendable)
            self.assertEqual(
                [f.code for f in verdict.refusals],
                [pa.Refusal.SCHEMA_VERSION_CHANGED],
            )


class DerivedFieldsAreNotAmendments(unittest.TestCase):
    def test_a_depth_change_alone_is_not_a_change(self):
        """Depth comes from the graph, not from an author.

        Adding an unrelated node can lift a depth without changing what any
        node was asked to do; convicting that would refuse safe amendments over
        a number nobody wrote.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _Run(tmp, [_agent("a")])
            self.addCleanup(run.store.close)
            run.merge("a")

            verdict = run.classify([replace(run.plan[0], depth=7)])

            self.assertTrue(verdict.amendable, verdict.as_mapping())
            self.assertEqual(verdict.changed, ())


class UnknownStateFailsClosed(unittest.TestCase):
    def test_a_node_with_no_recorded_state_is_treated_as_unstarted(self):
        """Safe direction: the rules only add refusals as a node progresses.

        An unknown state can therefore never turn a refusal into a permission —
        it can only decline to add one, and the settled/running checks that
        matter are the ones a known state would have triggered.
        """
        verdict = pa.classify(
            [_agent("a")], [replace(_agent("a"), instruction="changed")], {}
        )
        self.assertTrue(verdict.amendable)
        self.assertEqual(verdict.changed, ("a",))


if __name__ == "__main__":
    unittest.main()
