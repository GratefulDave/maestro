"""A failure raised before the launch is a question about nothing.

`run_node` does real work before it can create anything: it renders a handoff,
size-checks it against the lane's window (B13), and resolves that window through
the route's model catalog. Every one of those can raise, and none of them has
opened a pane or started a process when it does.

The runtime's quiescer resolves an attempt's handle from a map that `run_node`
populates only *after* a launch has succeeded, so all of those failures reach it
with no handle. For as long as its only answer there was
`PROCESS_GROUP_UNTRACKED` — "absence unproven" — the scheduler turned that answer
into a terminal `QUIESCENCE_UNPROVEN` about a process group that was never
created, and the failure that actually happened survived only as a string in the
block detail.

That is `lane-routing-chemical-tests` in run-8d1a71f463e4430f92a125a8f8b3731d:

    RuntimeError: PROCESS_GROUP_UNTRACKED:pre-inventory:lane-routing-chemical-tests#1
    HandoffTooLarge: model 'deepseek/deepseek-v4-flash:auto' does not resolve
                     in the omp catalog ... (B13)
    ValueError:      model pattern 'deepseek/deepseek-v4-flash:auto' not found

with `launched_at`, `pid`, and `attempt_host` all NULL on attempt 1 — the durable
record of a lane that blocked terminally without ever being dispatched.

`scheduler._launch_left_nothing_to_reap` covers the neighbouring half of this
shape and only that half: one exception type, `LaunchFailed`, carrying one typed
field, `pane_created` (§16.3 item 45). It cannot cover a `HandoffTooLarge`, and
widening it to every failed launch is exactly what that item forbids. The frame
that knows is the runtime's, and what it knows is structural: whether this
process has entered the one call that can create anything for this attempt.

Both arms are driven below, over the production `run_node` and `quiesce_attempt`
that `maestro._run_start` builds:

* a pre-dispatch failure is provably absent — for any node kind, and for each of
  the two sites that raise before the launch;
* a writer that may exist still owes a measured absence, and a writer that
  demonstrably still owns its group still blocks.
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

import maestro  # noqa: E402
from adw_modules import code_review  # noqa: E402
from adw_modules import launcher  # noqa: E402
from adw_modules import scheduler_types  # noqa: E402


TESTS_NODE = SimpleNamespace(
    kind=scheduler_types.NodeKind.TESTS,
    node_id="lane-any-tests",
    node_ids=(),
    instruction="write the failing cases",
    outputs=("tests/test_any.py",),
    gate=None,
    needs=("lane-any",),
    # Non-None so `_refuse_uncontracted_tests_nodes` admits the run. Its
    # contents are not the subject here; its presence is what the start-side
    # rollout invariant requires of any plan carrying a tests node.
    test_strength=SimpleNamespace(),
)
AGENT_NODE = SimpleNamespace(
    kind=scheduler_types.NodeKind.AGENT,
    node_id="lane-any",
    instruction="do the work",
    outputs=("src/any.py",),
    needs=(),
)
CODE_NODE = SimpleNamespace(
    kind=scheduler_types.NodeKind.CODE,
    node_id="lane-code",
    command=("true",),
)


class _Handle(launcher.LaunchHandle):
    pass


class RecordingRoute:
    """§9.3's six adapter operations, with the launch under the test's control."""

    workspace_label = "ws"

    def __init__(self, *, raises=None, owned=()):
        self._raises = raises
        self._owned = owned
        self.launched = []
        self.cancelled = []

    def launch(self, spec):
        self.launched.append(spec)
        if self._raises is not None:
            raise self._raises
        return launcher.LaunchHandle(
            spec.correlation_token,
            "pane-1",
            "agent-1",
            spec.worktree,
            transcript_path=spec.session_dir / "session.jsonl",
        )

    def poll(self, _handle):
        return launcher.PollResult(launcher.PollState.EXITED, 0, "ok")

    def cancel(self, handle, _deadline):
        self.cancelled.append(handle.correlation_token)

    def reclaim(self, _token):
        return self._owned

    def provision(self, _worktree):
        return None

    def wait_for_idle(self, _handle):
        return None


def drive(body, *, route=None, patches=()):
    """Run `body(deps)` against the deps `maestro._run_start` actually builds.

    A fixture that reimplemented `run_node` or `quiesce_attempt` would pass
    under either implementation of the seam, which is the whole subject here.
    """
    route = route if route is not None else RecordingRoute()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "run"
        scratch = root / "scratch"
        (scratch / "session").mkdir(parents=True)
        plan = SimpleNamespace(
            agent_nodes=(AGENT_NODE,),
            tests_nodes=(TESTS_NODE,),
            merge_policy=SimpleNamespace(
                integration_branch="main",
                integration_gate=SimpleNamespace(runner="none", argv=(), min_cases=1),
            ),
            node_by_id=lambda: {
                TESTS_NODE.node_id: TESTS_NODE,
                AGENT_NODE.node_id: AGENT_NODE,
                CODE_NODE.node_id: CODE_NODE,
            },
            to_plan_nodes=lambda: (),
        )
        attempt = SimpleNamespace(path=root, scratch=scratch)
        outcome = {}

        class CapturingScheduler:
            def __init__(self, _run_id, _nodes, _config, deps, **_kwargs):
                self.deps = deps

            def project(self):
                return None

            def run(self):
                outcome["result"] = body(self.deps, attempt)
                return SimpleNamespace(
                    outcome=scheduler_types.RunOutcome.ACCEPTED, merged=(), blocked=()
                )

        args = SimpleNamespace(
            plan_file=str(root / "plan.json"),
            db=str(Path(tmp) / "state.db"),
            run_id="run-1",
            integration_path=str(root),
            repo=str(root),
            data_dir=str(root / "data"),
            receipt_dir=str(root / "receipts"),
            worktrees_root=str(root / "worktrees"),
            scratch_root=str(root / "scratch-root"),
            digest="a" * 64,
            agent_route="omp",
            agent_model="x-ai/grok-4.6",
            agent_effort="high",
            agent_profile="grok",
            tester_route="omp",
            tester_model="x-ai/grok-4.6",
            tester_effort="high",
            tester_profile="deepseek",
            tester_vendor="deepseek",
        )
        stack = [
            mock.patch.object(maestro, "_run_configuration", return_value=mock.Mock()),
            mock.patch.object(maestro, "_load_runnable_plan", return_value=plan),
            mock.patch.object(maestro, "_resolve_run_runners", return_value={}),
            mock.patch.object(maestro, "_validate_run_paths"),
            mock.patch.object(maestro, "_runtime_launcher", return_value=route),
            mock.patch.object(maestro.lc, "LifecycleStore", return_value=mock.Mock()),
            mock.patch.object(maestro.scheduler, "Scheduler", CapturingScheduler),
            *patches,
        ]
        output = io.StringIO()
        with contextlib.ExitStack() as entered:
            for patch in stack:
                entered.enter_context(patch)
            entered.enter_context(contextlib.redirect_stdout(output))
            code = maestro._run_start(args)
        assert code == 0, output.getvalue()
        return outcome["result"], route


def refused(record, deps, attempt, node, phase="pre-inventory"):
    """Fail the launch the way production does, then take the proof.

    Returns `(the failure run_node raised, the failure the quiesce raised)` —
    the second one `None` when the quiescer answered rather than raised, which
    is the whole difference this file is about.
    """
    launch_failure = None
    quiesce_failure = None
    try:
        deps.run_node(attempt, node, record, "", lambda _pid=None: None, lambda: False)
    except BaseException as exc:  # noqa: BLE001 - the subject of the test
        launch_failure = exc
    try:
        deps.quiesce_attempt(record, phase)
    except BaseException as exc:  # noqa: BLE001
        quiesce_failure = exc
    return launch_failure, quiesce_failure


def record_for(node, attempt_no=1):
    return SimpleNamespace(node_id=node.node_id, attempt_no=attempt_no)


# ── the incident: a pre-dispatch refusal is absent by construction ──────────


class PreDispatchFailureIsProvablyAbsentTests(unittest.TestCase):
    def test_a_tests_node_refused_by_the_size_check_is_not_quiescence_unproven(self):
        """The observed shape, over an arbitrary tests node.

        `_preflight_prompt` is B13 at the CLI and it raises before the prompt
        is even written to disk, let alone dispatched. Under the old quiescer
        this attempt blocked QUIESCENCE_UNPROVEN and the refusal below reached
        the ledger only as a string.
        """
        refusal = code_review.HandoffTooLarge(
            "model 'deepseek/deepseek-v4-flash:auto' does not resolve in the "
            "omp catalog, so the prompt cannot be shown to fit any window (B13)"
        )
        (launch_failure, quiesce_failure), route = drive(
            lambda deps, attempt: refused(
                record_for(TESTS_NODE), deps, attempt, TESTS_NODE
            ),
            patches=[
                mock.patch.object(maestro, "_preflight_prompt", side_effect=refusal)
            ],
        )
        self.assertIs(launch_failure, refusal)
        self.assertIsNone(
            quiesce_failure,
            "a proof about a process group that was never created must not "
            "outrank the failure that caused it",
        )
        self.assertEqual(route.launched, [])

    def test_the_window_lookup_inside_the_spec_is_inside_the_same_window(self):
        """The second site, and the reason the launch spec is a named local.

        `_route_context_window` resolves the lane's model through its route's
        catalog and raises when it does not resolve. As an inline argument to
        `LaunchSpec(...)` that raise happened after a mark cleared for the
        launch and before the launch was entered — in the one gap a
        chokepoint-shaped fix would leave open.
        """
        refusal = code_review.HandoffTooLarge("no window for this lane (B13)")
        (launch_failure, quiesce_failure), route = drive(
            lambda deps, attempt: refused(
                record_for(TESTS_NODE), deps, attempt, TESTS_NODE
            ),
            patches=[
                mock.patch.object(maestro, "_preflight_prompt", return_value=1),
                mock.patch.object(
                    maestro, "_route_context_window", side_effect=refusal
                ),
            ],
        )
        self.assertIs(launch_failure, refusal)
        self.assertIsNone(quiesce_failure)
        self.assertEqual(route.launched, [])

    def test_the_exemption_is_not_a_property_of_the_node_kind(self):
        """Nothing here is about tests nodes. `launch_env` is the first
        statement in `run_node` and precedes the kind switch entirely."""
        for node in (TESTS_NODE, AGENT_NODE, CODE_NODE):
            with self.subTest(kind=node.kind.value):
                boom = OSError("scratch redirect unavailable")
                (launch_failure, quiesce_failure), _ = drive(
                    lambda deps, attempt, node=node: refused(
                        record_for(node), deps, attempt, node
                    ),
                    patches=[
                        mock.patch.object(
                            maestro.worktree, "launch_env", side_effect=boom
                        )
                    ],
                )
                self.assertIs(launch_failure, boom)
                self.assertIsNone(quiesce_failure)

    def test_the_exemption_is_not_a_property_of_the_phase(self):
        """`settle` and `cancel` reach the same undispatched attempt and get
        the same answer. A phase allowlist would have covered `pre-inventory`
        and left the next phase to rediscover this incident."""
        refusal = code_review.HandoffTooLarge("no window for this lane (B13)")
        for phase in ("pre-inventory", "settle", "cancel", "candidate-idle"):
            with self.subTest(phase=phase):
                (_, quiesce_failure), _ = drive(
                    lambda deps, attempt, phase=phase: refused(
                        record_for(TESTS_NODE), deps, attempt, TESTS_NODE, phase=phase
                    ),
                    patches=[
                        mock.patch.object(
                            maestro, "_preflight_prompt", side_effect=refusal
                        )
                    ],
                )
                self.assertIsNone(quiesce_failure)


# ── the half a widened exemption gets wrong ─────────────────────────────────


class AWriterThatMayExistStillOwesTheProofTests(unittest.TestCase):
    def test_a_launch_that_reached_the_chokepoint_still_demands_absence(self):
        """A refusal raised *inside* the launch may have created a pane, and
        the runtime has no handle for it. `PROCESS_GROUP_UNTRACKED` is the
        correct answer there and must survive this change."""
        route = RecordingRoute(raises=launcher.LaunchRefused(launcher.LaunchRefusal.NO_PANE))
        (launch_failure, quiesce_failure), route = drive(
            lambda deps, attempt: refused(
                record_for(TESTS_NODE), deps, attempt, TESTS_NODE
            ),
            route=route,
        )
        self.assertIsNotNone(launch_failure)
        self.assertIsInstance(quiesce_failure, RuntimeError)
        self.assertIn("PROCESS_GROUP_UNTRACKED", str(quiesce_failure))
        self.assertEqual(len(route.launched), 1)

    def test_a_live_agent_still_blocks(self):
        """The dispatched case, unchanged: a route that still owns the
        correlation token after `cancel` is a writer that may still be writing,
        and quiescence is exactly what refuses to proceed past it."""
        (result, quiesce_failure), route = drive(
            lambda deps, attempt: refused(
                record_for(TESTS_NODE),
                deps,
                attempt,
                TESTS_NODE,
                phase="candidate-idle",
            ),
            route=RecordingRoute(owned=("run-1-lane-any-tests-1",)),
        )
        self.assertIsInstance(quiesce_failure, RuntimeError)
        self.assertIn("PROCESS_GROUP_STILL_OWNED", str(quiesce_failure))
        self.assertEqual(len(route.launched), 1)

    def test_a_live_code_node_process_still_blocks(self):
        """The other branch of the same quiescer, over a process group that
        `_process_group_absent` says is still there."""

        class LiveProcess:
            pid = 4321
            returncode = 0

            def __init__(self, *_args, **_kwargs):
                pass

            def poll(self):
                return 0

        (result, quiesce_failure), _ = drive(
            lambda deps, attempt: refused(
                record_for(CODE_NODE), deps, attempt, CODE_NODE, phase="settle"
            ),
            patches=[
                mock.patch.object(maestro.subprocess, "Popen", LiveProcess),
                mock.patch.object(maestro.launcher, "quiesce_process_group"),
                mock.patch.object(
                    maestro.launcher, "_process_group_absent", return_value=False
                ),
            ],
        )
        self.assertIsInstance(quiesce_failure, RuntimeError)
        self.assertIn("PROCESS_GROUP_STILL_OWNED", str(quiesce_failure))

    def test_a_code_node_that_started_a_process_is_never_exempt(self):
        """Fail-closed at the code chokepoint: the mark is cleared before
        `Popen`, so a raise from inside it keeps owing the proof rather than
        inheriting an exemption measured before the call."""

        def exploding(*_args, **_kwargs):
            raise OSError("fork failed after the child was spawned")

        (launch_failure, quiesce_failure), _ = drive(
            lambda deps, attempt: refused(
                record_for(CODE_NODE), deps, attempt, CODE_NODE
            ),
            patches=[mock.patch.object(maestro.subprocess, "Popen", exploding)],
        )
        self.assertIsInstance(launch_failure, OSError)
        self.assertIsInstance(quiesce_failure, RuntimeError)
        self.assertIn("PROCESS_GROUP_UNTRACKED", str(quiesce_failure))


if __name__ == "__main__":
    unittest.main()
