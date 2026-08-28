"""Executable proof that a REJECTED tests node can actually be repaired.

§19 M41 gave a `tests` node the full review **and repair** apparatus. Only
the review half shipped. The repair half ended in two guards inside
`maestro._execute_run`'s `continue_node` closure, both keyed on
`NodeKind.AGENT`:

  1. `if node.kind is not NodeKind.AGENT: raise` -- reached first, so
     `write_prompt`, the only writer of `repair-prompt.md`, never ran and the
     reviewer's findings reached nobody.
  2. `if node.kind is NodeKind.AGENT: store.register_actor_session(...)` in
     `run_node`. A tester launches with `pane_role="tester"` and registers no
     durable actor session, so past guard 1 the closure raised
     `"builder generation changed"` instead. Deleting guard 1 alone reproduces
     the identical symptom -- two guards, one gap.

Both raised `AttemptOwnershipLost`, which the worker's own containment
handler turned into a bare `return`: no transition, no `fail_handoff`, no
log. `run-36dd33d262d9485ca815aea5001b2ce2`'s `lane-wp6-tests` wrote its
`REPAIRING` phase and then nothing at all, while `attempts` and
`node_lifecycle` kept reading RUNNING for a lane with no process.

Every test here drives the **real** `continue_node` closure, captured out of
`_execute_run`. That matters: every pre-existing repair test in this suite
injects `deps(continue_node=...)`, and those fakes accept any node kind, so
the real closure had never been executed against a `tests` node by anything.

  P1  a tests-node handoff is serviced by a fresh tester in the same worktree
  P2  the repair launches as the *tester*, on the tester's route
  P3  a handoff nothing can service is a typed ledger transition, and a
      genuine cancel still is not
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


FINDINGS = "Assert complete expected pairs, and an observable resolver result"


class _NoProgress:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _RecordingRunner:
    """Records every `LaunchSpec` and hands back a usable handle."""

    def __init__(self, root: Path):
        self.root = root
        self.launched = []
        self.cancelled = []

    def launch(self, spec):
        self.launched.append(spec)
        return lch.LaunchHandle(
            correlation_token=spec.correlation_token,
            pane_id="tester-pane",
            agent_name="tester",
            launched_cwd=spec.worktree,
            transcript_path=self.root / "tester.jsonl",
            environment=spec.environment,
        )

    def adopt(self, _persisted):
        raise AssertionError("a tests node has no durable session to adopt")

    def cancel(self, handle, _deadline):
        self.cancelled.append(handle)

    def reclaim(self, _token):
        return False

    def provision(self, _worktree):
        return None


@contextlib.contextmanager
def captured_continue_node(*, kind=st.NodeKind.TESTS, preflight=None):
    """Yield the real `continue_node` closure plus everything it wrote to.

    `_execute_run` is entered for real; only the seams that would need a
    pane, a catalog, or a database are replaced. The closure that comes back
    is the production one, byte for byte.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"
        integration = root / "integration"
        scratch = root / "scratch"
        for path in (repo, integration, scratch):
            path.mkdir()
        args = SimpleNamespace(
            plan_file=str(root / "plan.json"),
            db=str(root / "state.sqlite3"),
            run_id="run-1",
            integration_path=str(integration),
            repo=str(repo),
            data_dir=str(root / "data"),
            receipt_dir=str(root / "receipts"),
            worktrees_root=str(root / "worktrees"),
            scratch_root=str(scratch),
            digest="d" * 64,
            agent_route="omp",
            agent_model="build-model",
            agent_effort="high",
            agent_profile="build-profile",
            execution_vendor="build-vendor",
            tester_route="claude",
            tester_model="test-model",
            tester_effort="low",
            tester_profile="test-profile",
            tester_vendor="test-vendor",
            concurrency=None,
            restrict_actor_tools=False,
        )
        tests_node = SimpleNamespace(
            node_id="lane-wp6-tests", kind=st.NodeKind.TESTS, needs=()
        )
        # The build lane that consumes them, so `_lane_placement_by_node`
        # places the tester in the *build* lane's tab rather than its own.
        build_node = SimpleNamespace(
            node_id="lane-wp6", kind=st.NodeKind.AGENT, needs=("lane-wp6-tests",)
        )
        plan = SimpleNamespace(
            title="plan",
            agent_nodes=(build_node,),
            tests_nodes=(tests_node,),
            merge_policy=SimpleNamespace(
                integration_branch="main",
                integration_gate=SimpleNamespace(min_cases=1),
            ),
            to_plan_nodes=lambda: (),
        )
        store = mock.Mock()
        store.repair_handoff.return_value = SimpleNamespace(
            builder_generation=1,
            state=st.RepairHandoffState.PENDING,
        )
        store.mark_handoff_submitted.return_value = SimpleNamespace(
            submitted=True, handoff=SimpleNamespace(builder_generation=1)
        )
        captured = {}

        class CapturingScheduler:
            def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                captured["deps"] = deps

            def project(self):
                return None

            def run(self):
                return SimpleNamespace(
                    outcome=SimpleNamespace(value="ACCEPTED"),
                    merged=(),
                    blocked=(),
                    review_findings={},
                )

        runner = _RecordingRunner(root)
        execution = SimpleNamespace(ok=True)
        preflight_calls = []
        # Lets a test act *during* the actor's turn -- writing the
        # acknowledgement, say -- which is the only moment it can, because the
        # closure clears the previous turn's artifacts before it launches.
        poll_hook = {"fn": None}

        def fake_poll(*_args, **_kwargs):
            if poll_hook["fn"] is not None:
                poll_hook["fn"]()
            return execution

        def fake_preflight(text, route, model):
            preflight_calls.append((text, route, model))
            if preflight is not None:
                return preflight(text, route, model)
            return None

        with (
            mock.patch.object(
                maestro, "_run_configuration", return_value=mock.Mock()
            ),
            mock.patch.object(maestro, "_load_runnable_plan", return_value=plan),
            mock.patch.object(maestro, "_refuse_cross_run_node_budget"),
            mock.patch.object(maestro, "_validate_run_paths"),
            mock.patch.object(maestro, "_resolve_run_runners", return_value={}),
            mock.patch.object(maestro, "_runtime_launcher", return_value=runner),
            mock.patch.object(
                maestro, "_refuse_base_commit_divergence", return_value=None
            ),
            mock.patch.object(
                maestro, "_refuse_uncommittable_outputs", return_value=None
            ),
            mock.patch.object(
                maestro, "_refuse_uncontracted_tests_nodes", return_value=None
            ),
            mock.patch.object(maestro, "_RunProgress", _NoProgress),
            mock.patch.object(maestro.lc, "LifecycleStore", return_value=store),
            mock.patch.object(maestro.scheduler, "Scheduler", CapturingScheduler),
            mock.patch.object(maestro, "_poll_agent_execution", fake_poll),
            mock.patch.object(maestro, "_route_context_window", return_value=None),
            mock.patch.object(maestro, "_preflight_prompt", fake_preflight),
            mock.patch.object(maestro.worktree, "launch_env", return_value={}),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                assert maestro._execute_run(args, resuming=False) == 0
            node = tests_node if kind is st.NodeKind.TESTS else build_node
            node = SimpleNamespace(
                node_id=node.node_id, kind=kind, needs=node.needs
            )
            attempt = SimpleNamespace(path=repo, scratch=scratch, repo=repo)
            # `node_id` as well as `attempt_no`: `quiesce_attempt` keys on the
            # record, `continue_tests_node` on the node, and in production
            # they are the same lane.
            record = SimpleNamespace(node_id=node.node_id, attempt_no=1)
            yield SimpleNamespace(
                continue_node=captured["deps"].continue_node,
                attempt=attempt,
                node=node,
                record=record,
                scratch=scratch,
                runner=runner,
                store=store,
                execution=execution,
                preflight_calls=preflight_calls,
                poll_hook=poll_hook,
                prompt_path=scratch / "repair-prompt.md",
                acknowledgement_path=scratch / "repair-acknowledgement.json",
            )


class TestsNodeRepairIsServiced(unittest.TestCase):
    """P1 — the handoff reaches a tester instead of raising."""

    def _repair(self, harness):
        return harness.continue_node(
            harness.attempt,
            harness.node,
            harness.record,
            f"Repair rejected candidate.\n\n{FINDINGS}",
            "a" * 40,
            1,
            lambda: False,
        )

    def test_a_tests_node_repair_handoff_is_serviced(self):
        """The condition that produced a lane with no process and no row."""
        with captured_continue_node() as harness:
            repair = self._repair(harness)
        self.assertIs(repair.execution, harness.execution)
        self.assertEqual(repair.builder_generation, 1)
        self.assertEqual(len(harness.runner.launched), 1)

    def test_the_repair_prompt_is_written_and_carries_the_findings(self):
        """`repair-prompt.md` had exactly one writer, below the guard."""
        with captured_continue_node() as harness:
            self._repair(harness)
            text = harness.prompt_path.read_text(encoding="utf-8")
            spec = harness.runner.launched[0]
            submitted = Path(spec.prompt_path).read_text(encoding="utf-8")
        self.assertIn(FINDINGS, text)
        # Not merely written: written to the file the launch submits.
        self.assertEqual(submitted, text)
        self.assertIn("a" * 40, text)
        self.assertIn("repair_acknowledgement", text)

    def test_the_prompt_does_not_claim_a_session_the_tester_never_had(self):
        """A one-shot tester's continuity is the worktree, not a session."""
        with captured_continue_node() as harness:
            self._repair(harness)
            text = harness.prompt_path.read_text(encoding="utf-8")
        self.assertNotIn("existing worktree and session", text)
        self.assertIn("fresh actor turn in the existing worktree", text)

    def test_the_durable_handoff_is_marked_submitted(self):
        """Otherwise the row stays PENDING behind a lane that moved on."""
        with captured_continue_node() as harness:
            self._repair(harness)
            harness.store.mark_handoff_submitted.assert_called_once()
            kwargs = harness.store.mark_handoff_submitted.call_args.kwargs
        self.assertEqual(kwargs["builder_generation"], 1)

    def test_the_acknowledgement_decides_the_returned_sha(self):
        """§1.2 — the receipt is a typed file, never the tester's prose."""
        with captured_continue_node() as harness:
            repair = self._repair(harness)
            self.assertEqual(repair.acknowledged_rejected_sha, "")

            def acknowledge():
                harness.acknowledgement_path.write_text(
                    '{"builder_generation": 1, "kind": "repair_acknowledgement",'
                    ' "rejected_candidate_sha": "%s"}' % ("a" * 40),
                    encoding="utf-8",
                )

            harness.poll_hook["fn"] = acknowledge
            acknowledged = self._repair(harness)
        self.assertEqual(acknowledged.acknowledged_rejected_sha, "a" * 40)

    def test_a_stale_envelope_cannot_be_read_as_this_turn(self):
        """A fresh actor cannot own the rejected turn's declaration."""
        with captured_continue_node() as harness:
            envelope = harness.scratch / "agent-envelope.json"
            envelope.write_text('{"stale": true}', encoding="utf-8")
            self._repair(harness)
            self.assertFalse(envelope.exists())


class SecondRepairRoundDoesNotOrphanTheFirst(unittest.TestCase):
    """Repair rounds share one attempt, so they share `generation`."""

    def _repair(self, harness, sha):
        return harness.continue_node(
            harness.attempt,
            harness.node,
            harness.record,
            FINDINGS,
            sha,
            1,
            lambda: False,
        )

    def test_the_previous_repair_tester_is_proven_absent_first(self):
        """Otherwise its pane stays open with nothing naming it."""
        with captured_continue_node() as harness:
            self._repair(harness, "a" * 40)
            self.assertEqual(harness.runner.cancelled, [])
            self._repair(harness, "b" * 40)
            cancelled = list(harness.runner.cancelled)
            first = harness.runner.launched[0]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].correlation_token, first.correlation_token)

    def test_each_round_gets_its_own_pane_identity_and_session(self):
        """One token hashes to one Herdr agent id; two rounds need two."""
        with captured_continue_node() as harness:
            self._repair(harness, "a" * 40)
            self._repair(harness, "b" * 40)
            first, second = harness.runner.launched
        self.assertNotEqual(first.correlation_token, second.correlation_token)
        self.assertNotEqual(first.session_dir, second.session_dir)
        self.assertIn("a" * 12, first.correlation_token)
        self.assertIn("b" * 12, second.correlation_token)


class TestsRepairLaunchesAsATester(unittest.TestCase):
    """P2 — the right actor, on the right route, with the right label."""

    def _spec(self, harness):
        harness.continue_node(
            harness.attempt,
            harness.node,
            harness.record,
            FINDINGS,
            "a" * 40,
            1,
            lambda: False,
        )
        return harness.runner.launched[0]

    def test_the_repair_uses_the_tester_route_and_pane_role(self):
        with captured_continue_node() as harness:
            spec = self._spec(harness)
        self.assertEqual(spec.pane_role, "tester")
        self.assertEqual(spec.route, "claude")
        self.assertEqual(spec.model, "test-model")
        self.assertEqual(spec.effort, "low")
        self.assertEqual(spec.profile, "test-profile")

    def test_the_repair_lands_in_the_lanes_existing_tab(self):
        """`_tab_for` caches on `lane_key`; the build lane owns the tab."""
        with captured_continue_node() as harness:
            spec = self._spec(harness)
        self.assertEqual(spec.lane_key, "lane-wp6")
        self.assertEqual(spec.lane_label, "lane-wp6")

    def test_the_handoff_is_size_checked_against_the_testers_window(self):
        """B13 at the chokepoint every dispatched prompt crosses."""
        with captured_continue_node() as harness:
            self._spec(harness)
            calls = list(harness.preflight_calls)
        self.assertTrue(calls)
        text, route, model = calls[-1]
        self.assertIn(FINDINGS, text)
        self.assertEqual((route, model), ("claude", "test-model"))

    def test_a_refused_size_check_leaves_no_pane_behind(self):
        def refuse(_text, _route, _model):
            raise RuntimeError("HANDOFF_TOO_LARGE")

        with captured_continue_node(preflight=refuse) as harness:
            with self.assertRaises(RuntimeError):
                self._spec(harness)
            self.assertEqual(harness.runner.launched, [])


class LaneActorDispatch(unittest.TestCase):
    """P2's resolution, read by both the first attempt and the repair."""

    ARGS = SimpleNamespace(
        agent_route="omp",
        agent_model="build-model",
        agent_effort="high",
        agent_profile="build-profile",
        execution_vendor="build-vendor",
        tester_route="claude",
        tester_model="test-model",
        tester_effort="low",
        tester_profile="test-profile",
        tester_vendor="test-vendor",
    )

    def test_a_tests_lane_resolves_to_the_tester(self):
        dispatch = maestro._lane_actor_dispatch(self.ARGS, st.NodeKind.TESTS)
        self.assertEqual(
            (dispatch.route, dispatch.model, dispatch.effort, dispatch.vendor),
            ("claude", "test-model", "low", "test-vendor"),
        )

    def test_an_agent_lane_resolves_to_the_builder(self):
        dispatch = maestro._lane_actor_dispatch(self.ARGS, st.NodeKind.AGENT)
        self.assertEqual(
            (dispatch.route, dispatch.model, dispatch.effort, dispatch.vendor),
            ("omp", "build-model", "high", "build-vendor"),
        )

    def test_an_unconfigured_tester_falls_back_to_the_agent_route(self):
        args = SimpleNamespace(
            agent_route="omp",
            agent_model="build-model",
            agent_effort="high",
            agent_profile="build-profile",
        )
        dispatch = maestro._lane_actor_dispatch(args, st.NodeKind.TESTS)
        self.assertEqual((dispatch.route, dispatch.model), ("omp", "build-model"))
        self.assertIsNone(dispatch.vendor)


class UnserviceableHandoffIsTyped(unittest.TestCase):
    """P3 — the swallow that made all of this invisible."""

    def test_a_kind_with_no_repair_route_refuses_by_type(self):
        with captured_continue_node(kind=st.NodeKind.CODE) as harness:
            with self.assertRaises(sch.UnserviceableHandoff) as raised:
                harness.continue_node(
                    harness.attempt,
                    harness.node,
                    harness.record,
                    FINDINGS,
                    "a" * 40,
                    1,
                    lambda: False,
                )
            self.assertEqual(harness.runner.launched, [])
        self.assertIn("no repair route", str(raised.exception))

    def test_it_is_an_ownership_loss_so_every_reraise_still_carries_it(self):
        self.assertTrue(
            issubclass(sch.UnserviceableHandoff, sch.AttemptOwnershipLost)
        )

    def _scheduler(self):
        store = mock.Mock()
        store.pinned_test_strength_contract.return_value = (
            st.TestStrengthContract.LEGACY
        )
        store.get_node.return_value = SimpleNamespace(
            state=st.NodeState.RUNNING,
            attempt_no=1,
            lane_phase=st.LanePhase.REPAIRING,
        )
        node = st.PlanNode(
            node_id="lane-wp6-tests",
            kind=st.NodeKind.TESTS,
            depth=0,
            needs=(),
            outputs=("tests/a.test.ts",),
            specs=(),
            instruction="Write the tests.",
            gate_command=("vitest",),
            gate_selector="tests/a.test.ts",
        )
        root = Path(tempfile.mkdtemp())
        deps = sch.SchedulerDeps(
            store=store,
            repo=root,
            integration_path=root,
            integration_branch="main",
            worktrees_root=root,
            scratch_root=root,
            run_node=lambda *a, **k: None,
            run_gate=lambda *a, **k: None,
            run_integration_gate=lambda *a, **k: None,
            quiesce_attempt=lambda *a, **k: None,
            review_attempt=None,
            continue_node=None,
        )
        config = st.SchedulerConfig(
            concurrency=1,
            node_timeout_s=60.0,
            turn_timeout_s=60.0,
            final_acceptance_timeout_s=60.0,
            backstop_t_s=600.0,
            semantic_ceiling=3,
        )
        scheduler = sch.Scheduler("run-1", (node,), config, deps)
        record = SimpleNamespace(
            node_id="lane-wp6-tests", attempt_no=1, key=("run-1", "lane-wp6-tests", 1)
        )
        return scheduler, store, record

    def test_an_unserviceable_handoff_writes_a_typed_blocked_transition(self):
        """The bare `return` is what left the ledger ending at REPAIRING."""
        scheduler, store, record = self._scheduler()

        def body(_self, _node, context):
            context.record = record
            raise sch.UnserviceableHandoff("lane-wp6-tests: no repair route")

        with mock.patch.object(sch.Scheduler, "_attempt_body", body):
            scheduler._attempt("lane-wp6-tests")
        store.mark_blocked.assert_called_once()
        call = store.mark_blocked.call_args
        self.assertIs(call.args[2], st.BlockReason.REPAIR_HANDOFF_UNSERVICEABLE)
        detail = call.kwargs["detail"]
        self.assertEqual(detail["exception_type"], "UnserviceableHandoff")
        self.assertIn("no repair route", detail["reason"])
        self.assertEqual(detail["node_kind"], "tests")

    def test_a_genuine_cancel_still_writes_no_outcome(self):
        """`AttemptCancelled` shares the clause and must not start blocking."""
        scheduler, store, record = self._scheduler()

        def body(_self, _node, context):
            context.record = record
            raise sch.AttemptCancelled("cancelled")

        with mock.patch.object(sch.Scheduler, "_attempt_body", body):
            scheduler._attempt("lane-wp6-tests")
        store.mark_blocked.assert_not_called()

    def test_a_plain_ownership_loss_still_writes_no_outcome(self):
        """A superseded generation has nothing to record; only this one does."""
        scheduler, store, record = self._scheduler()

        def body(_self, _node, context):
            context.record = record
            raise sch.AttemptOwnershipLost("superseded")

        with mock.patch.object(sch.Scheduler, "_attempt_body", body):
            scheduler._attempt("lane-wp6-tests")
        store.mark_blocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
