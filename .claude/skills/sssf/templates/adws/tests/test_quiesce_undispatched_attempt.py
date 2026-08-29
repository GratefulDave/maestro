"""A cleanup must not outrank the failure it is cleaning up after (§19 M3).

§16.3 item 45 was discharged at one of the two quiesce sites a failed launch
crosses. `_attempt_body`'s `pre-inventory` proof learned to read the launcher's
typed `pane_created` and skip when nothing was created; `_attempt`'s
containment handler, one frame up, went on calling `_settle_context`
unconditionally. The runtime's quiescer resolves an attempt's handle from a map
the dispatch populates only after a *successful* launch, so the same refusal
met the same `PROCESS_GROUP_UNTRACKED` one phase later, and the node blocked on
the cleanup's complaint instead of classifying the launch failure underneath
it.

Observed on run-2a44d226e75a4be391a14f02b78a6d25, node
`lane-p1-freeze-and-run-log`, attempt 4, at **zero turns**: herdr split pane
`w13A:p29`, refused `agent start` into it with `agent_pane_busy`, the launcher
reaped the pane and said so in its type, and the node blocked
`QUIESCENCE_UNPROVEN` — terminal — taking every node downstream of it with it.
`agent_pane_busy` is in `TRANSIENT_HERDR_ERROR_CODES` and `AGENT_START_REFUSED`
is typed non-deterministic, so every part of the machinery that would have
retried it was present and unreachable.

The shape is wider than that one site. §8.3's proof is owed only where an
attempt could own execution, and an attempt this scheduler leased but never
dispatched owns none by construction — `run_node` is the sole opener of a pane
or a process for an attempt. The runtime encoded that truth for exactly one
phase (`pre-baseline` returns silently) and left every other phase to convict:
`settle` on the worktree-check and gate-not-falsifiable paths as well as on the
refused launch, each reaching an attempt that dispatched nothing.

The exemption stops at the settle for `pre-baseline` and `cancel`.
The watchdog's kill and `fail`'s `watchdog` quiesce now consult
`_attempt_dispatched`: an attempt that never entered `run_node` has
no pane to prove absent, and answering `PROCESS_GROUP_UNTRACKED`
there is terminal (lane-wp6-build#1). Those two skip; the other two
keep demanding the proof. `tests/test_scheduler.py::QuiescenceTests`
and `GenerationFenceTests` hold the controls.

Every skip below keeps its negative control. A refusal that may have left a
pane still demands the measured proof; a refusal typed deterministic still
blocks on its first occurrence having spent nothing; an untyped failure has
stated nothing and is quiesced exactly as before; and a transient refusal that
never succeeds still blocks once its budget is spent, so no skip here can hang
a run.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro                                   # noqa: E402
from adw_modules import launcher as lch          # noqa: E402
from adw_modules import retry_policy as rp       # noqa: E402
from adw_modules import scheduler as sch         # noqa: E402
from adw_modules import scheduler_types as st    # noqa: E402
from adw_modules import worktree as worktree_module  # noqa: E402
from adw_modules.route_receipts import (load_admitted_routes,  # noqa: E402
                                        load_public_key)

from test_launch_refusal_cleanup import FakeHerdr   # noqa: E402
from test_scheduler import SchedulerFixture, green  # noqa: E402


#: The refusal herdr returned, byte for byte, in
#: run-2a44d226e75a4be391a14f02b78a6d25. Kept verbatim because the whole point
#: of `HerdrCallError.code` is that the *message* is prose herdr may reword at
#: any release: the classification must come off the typed field parsed out of
#: this envelope, and a paraphrase would not prove that.
AGENT_PANE_BUSY_PAYLOAD = (
    'LAUNCH_REFUSED:{"error":{"code":"agent_pane_busy","message":"agent '
    'target pane w13A:p29 is not an available shell"},"id":"cli:agent:start"}')


def busy_pane_error() -> lch.HerdrCallError:
    """The payload as `HerdrCallError` carries it: message plus typed code."""
    return lch.HerdrCallError(
        AGENT_PANE_BUSY_PAYLOAD,
        lch.herdr_error_code(AGENT_PANE_BUSY_PAYLOAD.split(":", 1)[1]))


def wrapped(refusal: lch.LaunchRefusal, detail: str = "",
            failure: rp.LauncherFailure = rp.LauncherFailure.STARTUP,
            pane_created=None) -> sch.LaunchFailed:
    """A refusal as `maestro._typed_launch` delivers it to the scheduler.

    The chaining is the mechanism under test: `LaunchFailed.pane_created` reads
    the refusal off `__cause__`, so a wrapper built without `from` carries no
    typed statement at all and must fall back to demanding the proof.
    """
    try:
        raise lch.LaunchRefused(refusal, detail, pane_created=pane_created)
    except lch.LaunchRefused as exc:
        try:
            raise sch.LaunchFailed(failure, "{0}: {1}".format(
                type(exc).__name__, exc)) from exc
        except sch.LaunchFailed as wrapper:
            return wrapper


def busy_pane_refusal() -> sch.LaunchFailed:
    """The incident's refusal, in the shape the scheduler received it."""
    return wrapped(lch.LaunchRefusal.AGENT_START_REFUSED,
                   "HerdrCallError: " + AGENT_PANE_BUSY_PAYLOAD,
                   failure=rp.LauncherFailure.TRANSPORT, pane_created=False)


def a_config() -> st.SchedulerConfig:
    return st.SchedulerConfig(
        concurrency=1, node_timeout_s=60.0, turn_timeout_s=30.0,
        final_acceptance_timeout_s=60.0, backstop_t_s=600.0,
        semantic_ceiling=2)


# ── the incident's own payload, through the real launcher ───────────────────

class BusyPaneRefusalTests(unittest.TestCase):
    """`agent_pane_busy`, verbatim, from herdr's refusal to a retry budget."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("do the work")
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = load_public_key(fixtures / "route_receipts.pub")
        self.admitted = load_admitted_routes(
            {"omp": fixtures / "omp.json", "claude": fixtures / "claude.json"},
            verify_keys=(key,))
        self.closed = None

    def spec(self) -> lch.LaunchSpec:
        return lch.LaunchSpec(
            correlation_token="run1-lane_p1-4", worktree=self.worktree,
            prompt_path=self.prompt, envelope_path=self.root / "envelope.json",
            route="omp", model="openai-codex/gpt-5.6-sol", effort="high",
            profile="openai-performance", session_dir=self.root / "session",
            context_window_tokens=400_000,
            environment=worktree_module.launch_env(self.scratch))

    def refuse(self) -> sch.LaunchFailed:
        """Drive the real launcher to the real refusal, through the runtime's
        single launch path (`maestro._typed_launch_pane`)."""
        harness = lch.HerdrLauncher(
            herdr_path=self.root / "herdr", omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"), admitted_routes=self.admitted)
        fake = FakeHerdr(worktree=self.worktree,
                         transcript=self.root / "session.jsonl")
        fake.start_error = busy_pane_error()
        harness._herdr = fake
        # Zero window: this case drives the refusal the scheduler receives,
        # not the bounded re-offer that precedes it
        # (`test_agent_start_busy_retry.py` owns that).
        harness.agent_start_busy_window_s = 0.0
        with self.assertRaises(sch.LaunchFailed) as caught:
            maestro._typed_launch_pane(harness, self.spec())
        self.closed = fake.closed
        return caught.exception

    def test_the_payload_carries_herdrs_typed_code(self):
        """Read off `error.code`, never matched out of the message (§1.2)."""
        self.assertEqual(busy_pane_error().code, "agent_pane_busy")
        self.assertIn("agent_pane_busy", lch.TRANSIENT_HERDR_ERROR_CODES)

    def test_the_verbatim_payload_reaches_a_retryable_launcher_class(self):
        """The incident's refusal, end to end through the real launcher.

        §7.5 closes the retry classes at three and files a launcher refusal
        under LAUNCHER_TRANSIENT rather than ENVIRONMENTAL. What the incident
        turns on is that it lands in a class with a budget to spend, outside
        the zero-budget partition, so the node retries instead of blocking.
        """
        failed = self.refuse()
        # The launcher reaped the pane and said so in its type, which is the
        # field §8.3's quiesce step reads.
        self.assertEqual(self.closed, ["w0:p2"])
        self.assertFalse(failed.pane_created)
        self.assertTrue(sch._launch_left_nothing_to_reap(failed))
        self.assertIs(failed.classified_failure, rp.LauncherFailure.TRANSPORT)
        self.assertNotIn(failed.classified_failure,
                         rp.DETERMINISTIC_LAUNCHER_FAILURES)
        self.assertGreater(
            rp.launcher_retry_budget(a_config(), failed.classified_failure), 0)


# ── the runtime's quiescer, reproduced ──────────────────────────────────────

class RuntimeQuiescerFixture(SchedulerFixture):
    """`maestro.quiesce_attempt`'s own logic, over a handle map this test owns.

    A quiescer that always succeeds cannot see this defect at all, and one that
    always raises cannot see the successful attempt that must follow the retry.
    The runtime keys on a handle map the dispatch populates **only after a
    successful launch**, answers `PROCESS_GROUP_UNTRACKED` for every phase but
    `pre-baseline` when the key is missing, and remembers a proven absence so a
    settle after a proven `pre-inventory` is a no-op. Reproduced verb for verb.
    """

    def setUp(self) -> None:
        super().setUp()
        #: `(node_id, attempt_no)` for every attempt whose launch registered.
        self.handles = set()
        self.proven = set()

    def quiesce_attempt(self, record, phase):
        super().quiesce_attempt(record, phase)
        key = (record.node_id, record.attempt_no)
        if key in self.proven:
            return
        if key not in self.handles:
            if phase == "pre-baseline":
                return
            raise RuntimeError(
                "PROCESS_GROUP_UNTRACKED:{0}:{1}#{2}".format(
                    phase, record.node_id, record.attempt_no))
        self.handles.discard(key)
        self.proven.add(key)

    def launching(self, refusal_for):
        """A runner that registers a handle only where its launch succeeded."""
        inner = self.run_node

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            boom = refusal_for(record.attempt_no)
            if boom is not None:
                on_launch(None)
                raise boom
            self.handles.add((node.node_id, record.attempt_no))
            return inner(attempt, node, record, retry_prompt, on_launch,
                         cancel_requested)

        return run_node

    def phases(self, node_id="a"):
        return [phase for (_run, node, _no), phase in self.quiesce_calls
                if node == node_id]

    def blocked_rows(self, node_id="a"):
        return [t for t in self.store.audit_transitions("run1")
                if t.get("node_id") == node_id
                and t.get("to_state") == st.NodeState.BLOCKED.value]

    def retry_rows(self, node_id="a"):
        return [t for t in self.store.audit_transitions("run1")
                if t.get("node_id") == node_id
                and str(t.get("reason", "")).startswith("retry:")]


class SettleOverAFailedLaunchTests(RuntimeQuiescerFixture):
    """A settle over a launch that had already cleaned up after itself."""

    def test_a_transient_refusal_retries_instead_of_blocking(self):
        """The incident, driven end to end.

        Under the unguarded settle this node blocked QUIESCENCE_UNPROVEN on its
        first attempt with `retry_class` NULL, and every dependant was stranded
        behind it.
        """
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=self.launching(
                lambda no: busy_pane_refusal() if no == 1 else None))).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.state, st.NodeState.MERGED)
        self.assertEqual(record.attempt_no, 2)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(self.blocked_rows(), [])

    def test_the_cleanup_does_not_replace_the_launch_failure_as_the_cause(self):
        """The failure row names the refusal, never the quiescer's complaint."""
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=self.launching(
                lambda no: busy_pane_refusal() if no == 1 else None))).run()

        retries = self.retry_rows()
        self.assertTrue(retries)
        self.assertIn(st.RetryClass.LAUNCHER_TRANSIENT.value,
                      str(retries[0].get("reason")))
        self.assertIn("AGENT_START_REFUSED", str(retries[0]))
        self.assertNotIn("PROCESS_GROUP_UNTRACKED", str(retries[0]))

    def test_a_transient_refusal_still_blocks_once_its_budget_is_spent(self):
        """The termination control. No skip here may make a run unbounded."""
        report = self.schedule(
            [self.agent("a")], config=self.config(launcher_retries=1),
            deps=self.deps(run_node=self.launching(
                lambda _no: busy_pane_refusal()))).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.state, st.NodeState.BLOCKED)
        self.assertIs(record.block_reason,
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)
        self.assertEqual(record.attempt_no, 2)
        self.assertIsNot(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_deterministic_refusal_blocks_on_its_first_occurrence(self):
        """The negative control on retryability.

        A missing binary, profile, or credential, or a route that is not
        configured, is a property of the configuration and identical on every
        attempt. Making refusals unconditionally retryable would spend two
        launches that cannot differ and then report a budget that never existed
        (§16.3 item 46), so this must block at attempt 1 naming the refusal.
        """
        self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=self.launching(
                lambda _no: wrapped(
                    lch.LaunchRefusal.SCRATCH_REDIRECT_MISSING,
                    "TMPDIR,PYTEST_ADDOPTS")))).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.state, st.NodeState.BLOCKED)
        self.assertIs(record.block_reason, st.BlockReason.LAUNCH_REFUSED)
        self.assertIsNot(record.block_reason,
                         st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertEqual(record.attempt_no, 1)
        self.assertIn("SCRATCH_REDIRECT_MISSING", str(self.blocked_rows()[-1]))

    def test_a_refusal_that_may_have_left_a_pane_still_demands_the_proof(self):
        """The fail-closed control on the skip.

        `NO_PANE` is raised after the split with nothing closed, so a pane may
        really exist and its group is exactly what quiescence is for. Widening
        the skip to every launch failure would report an absence nobody
        measured, and QUIESCENCE_UNPROVEN is the correct answer when the
        harness cannot supply the proof.
        """
        self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=self.launching(
                lambda _no: wrapped(lch.LaunchRefusal.NO_PANE)))).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertIn("pre-inventory", self.phases())

    def test_an_untyped_runner_failure_is_quiesced_exactly_as_before(self):
        """The skip is scoped to a typed statement. Nothing else changes."""
        self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=self.launching(
                lambda _no: RuntimeError("boom")))).run()

        self.assertIn("pre-inventory", self.phases())
        record = self.store.get_node("run1", "a")
        self.assertIs(record.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)


# ── the same shape before anything was ever dispatched ──────────────────────

class QuiesceBeforeDispatchTests(RuntimeQuiescerFixture):
    """An attempt that never entered `run_node` owns nothing to prove absent."""

    def test_a_failure_before_dispatch_keeps_its_own_verdict(self):
        """§7.4's falsifiability verdict, reached before any launch.

        A green pre-gate settles the attempt without ever entering `run_node`,
        so the settle's quiesce is asked about an attempt that launched
        nothing. It answered PROCESS_GROUP_UNTRACKED and buried
        GATE_NOT_FALSIFIABLE under a terminal quiescence block.
        """
        self.gate_script = {("a", "pre"): [green()]}
        self.schedule([self.agent("a")], deps=self.deps()).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.block_reason,
                      st.BlockReason.GATE_NOT_FALSIFIABLE)
        self.assertIsNot(record.block_reason,
                         st.BlockReason.QUIESCENCE_UNPROVEN)

    def test_a_pre_dispatch_cancel_still_demands_the_proof(self):
        """The boundary of the exemption, and why it is where it is.

        §7.8's cancellation and §7.6's watchdog reach an attempt that has not
        dispatched but whose provision or pre-gate subprocess may be running
        this instant. Those are harness-owned execution the settle's reasoning
        says nothing about, so `cancel` keeps asking and keeps blocking when
        the runtime cannot answer.
        """
        scheduler = self.schedule([self.agent("a")], deps=self.deps())
        record = self._leased_but_undispatched(scheduler)

        with self.assertRaises(sch.QuiescenceFailure):
            scheduler._quiesce(record, "cancel")
        self.assertIn("cancel", self.phases())

    def test_a_pre_dispatch_settle_asks_for_no_proof(self):
        """The exemption itself, at the phase it is scoped to."""
        scheduler = self.schedule([self.agent("a")], deps=self.deps())
        record = self._leased_but_undispatched(scheduler)
        self.assertFalse(scheduler._attempt_dispatched(record))

        context = sch._AttemptContext(record=record)
        scheduler._settle_context(context)
        self.assertTrue(context.settled)
        self.assertNotIn("settle", self.phases())

    def test_an_attempt_this_scheduler_never_leased_still_demands_the_proof(self):
        """Fail-closed. An inherited RUNNING row from another process owns
        execution this scheduler cannot account for, and not knowing about it
        is not evidence that it is absent."""
        scheduler = self.schedule([self.agent("a")], deps=self.deps())
        inherited = st.AttemptRecord(
            run_id="run1", node_id="a", attempt_no=7, base_sha="deadbeef")
        self.assertTrue(scheduler._attempt_dispatched(inherited))

    def _leased_but_undispatched(self, scheduler) -> st.AttemptRecord:
        """One attempt leased exactly as `_attempt_body` leases it, no further.

        The dispatch ledger is written where the durable row is created, so
        this reproduces the state every pre-dispatch quiescer meets: a RUNNING
        attempt with no pane, no process, and no handle anywhere.
        """
        scheduler.project()
        attempt_no = self.store.start_attempt("run1", "a", "0" * 40)
        with scheduler._lock:
            scheduler._attempt_dispatch.setdefault(("a", attempt_no), False)
        return self.store.get_attempt("run1", "a", attempt_no)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
