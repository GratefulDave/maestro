"""The DAG scheduler — §12.2 step 6, the piece that makes the rest run.

Everything before this step was a part. The lifecycle store knows how to
write a transition but not when; the classifier knows what a failure means
but never sees one; the measurement bracket knows how to commit a delta but
nothing calls it. This module is the composition, and composing is where
this design has been wrong every time it was reasoned about rather than run.

The order of an attempt is not an implementation detail — several of the
design's guarantees are properties of the order alone, so it is written out
once, here, and every one of its steps is asserted by a test:

    PENDING -> RUNNING              # §7.6 — before anything, so the attempt
                                    #   window covers the pre-launch segment
    create the attempt worktree     # §8.1 — from the integration head
    check_at_create                 # §8.3's four checks, first evaluation
    pre-node gate                   # §7.4 — must FAIL, or GATE_NOT_FALSIFIABLE
    take_baseline                   # §8.3 — the bracket opens
    run the node                    # the agent, or the code node's command
    inventory / delta               # §8.3 — the bracket closes
    permission_check                # §7.3 clause 4, at measurement
    commit_measured_delta           # §8.4 — before the gate, deliberately
    check_post_commit               # §8.3, against the expected inventory
    post-node gate                  # §7.3 clause 3, against the committed tree
    RUNNING -> VERIFIED             # only if all four clauses hold
    merge by output SHA             # §8.5, §8.6 — in (depth, node_id) order

Four seams are injected rather than implemented, because each belongs to
step 7's launcher and would be a lie if faked here (§12.3 — a deferral is
loud, never a stub): running a node, running a gate, running the integration
gate, and proving an owned attempt's process groups absent. They are injected
behind cancellable protocols the real adapters implement, so the offline suite
exercises this module's own logic rather than a mock of somebody else's.

Two decisions worth stating because they are not obvious from the spec, and
both were forced by reading the code rather than the document:

* **Budget exhaustion is decided at failure time, while the node still holds
  its attempt.** The store's `mark_blocked` legally transitions only from
  RUNNING, and it is right to: a node blocks out of an attempt, not out of
  the queue. Deciding at next pick-up instead would find the node PENDING
  and the transition refused. So the worker classifies, checks the budget,
  and either blocks now or releases the node to PENDING for another attempt.
* **The merge is serialized on one thread**, never inside a worker. §8.5's
  order is a function of the graph, so merging wherever a node happened to
  finish would make the sequence depend on timing — the exact property the
  frontier exists to remove.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence,
                    Tuple)

from . import code_review as cr
from . import lifecycle as lc
from . import retry_policy as rp
from . import scheduler_types as st
from . import verification as vf
from . import watchdog as wd
from . import worktree as wt


@dataclass
class NodeExecution:
    """What an adapter reports back about one node's execution.

    Deliberately structural. There is no stdout or stderr field, for the same
    reason `retry_policy.FailureSignal` has none: what the scheduler may
    learn from an execution is whether it produced a parseable envelope, what
    it exited, and whether it launched — never what it printed.

    **`launch_detail` widens that on purpose, and the boundary it does not
    cross is the point.** §16.3 item 42 left the ruling open rather than
    adding a field silently: the launcher's typed poll vocabulary was
    discarded on every branch of the real adapter, so a vanished agent and a
    refused launch reached the ledger as the same empty ENVIRONMENTAL row.
    The ruling taken here splits that vocabulary by what may read it.

    * `launcher_failure` is TYPED and drives classification. It is a
      `retry_policy.LauncherFailure` member, derived by the adapter from
      `launcher.ErrorClass`, itself derived from the exception's *type* —
      which §7.5 names explicitly among the facts a classifier may read.
    * `launch_detail` is PROSE and drives nothing. It reaches
      `Classification.reason`, and from there `transitions.detail_json`, and
      no guard reads it back. It exists because §7.6 already established that
      a typed reason computed at the point of failure and dropped before the
      ledger is indistinguishable, to every later reader, from no reason.

    Keying a retry class on `launch_detail` would be the lexical shortcut
    §7.5 forbids, and the launcher already settled the same question for
    itself (`HerdrCallError`: branch on `.code`, never on the message). This
    field is carried, recorded, and never branched on.
    """

    envelope_parsed: bool = True
    exit_code: int = 0
    launched_pid: Optional[int] = None
    launcher_failure: Optional[rp.LauncherFailure] = None
    launch_detail: str = ""


#: Runner boundaries receive the scheduler's cancellation lease explicitly.
#: They MUST use it while waiting so no gate or owned execution can outlive
#: the RUNNING generation that authorized it.
CancelRequested = Callable[[], bool]
NodeRunner = Callable[[
    wt.AttemptWorktree, st.PlanNode, st.AttemptRecord, Optional[str],
    Callable[[Optional[int]], None], CancelRequested,
], NodeExecution]
GateRunner = Callable[[
    wt.AttemptWorktree, st.PlanNode, str, CancelRequested,
], "wt.GateResult"]
IntegrationGateRunner = Callable[[
    Path, Sequence[str], CancelRequested,
], "wt.GateResult"]
QuiesceAttempt = Callable[[st.AttemptRecord, str], None]
#: `(attempt, node, record, base_sha, output_sha) -> code_review.ReviewOutcome`.
#: Raises `code_review.ReviewStalled` when the reviewer never reported, which
#: the scheduler classifies ENVIRONMENTAL — a wedged reviewer is a fact about
#: the machine, and must not spend a node's review budget.
ReviewRunner = Callable[[
    wt.AttemptWorktree, st.PlanNode, st.AttemptRecord, str, str,
], Any]


class QuiescenceDependencyError(ValueError):
    """A scheduler without process-absence proof is unsafe to construct."""


class QuiescenceFailure(RuntimeError):
    """The attempt's owned execution could not be proven absent."""

    def __init__(self, phase: str, cause: BaseException) -> None:
        super().__init__(f"{phase}: owned execution quiescence is unproven")
        self.phase = phase
        self.__cause__ = cause


class AttemptOwnershipLost(RuntimeError):
    """A worker observed that its RUNNING generation was superseded."""


class AttemptCancelled(RuntimeError):
    """Cancellation was requested while the attempt still held RUNNING."""


class LaunchFailed(RuntimeError):
    """A launch or poll that produced no usable agent, typed at the seam.

    The runner adapter raises this rather than a bare `RuntimeError` so the
    containment handler can classify on the exception's *type* and a typed
    enum member instead of on its message. §7.5 permits `_classify` to read an
    exception type and forbids it reading process output; a message such as
    `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:...` carries its code in prose,
    and matching that prefix would be exactly the lexical shortcut forbidden.

    Before this existed every launcher fault reached the handler as some bare
    exception, `classify` found nothing structural, and the failure spent the
    ENVIRONMENTAL budget — which is how §16.3 item 42's reader-without-writer
    survived: the branch was present and nothing could ever reach it.

    `detail` is the underlying message, carried for the ledger and never
    branched on.
    """

    def __init__(self, failure: rp.LauncherFailure, detail: str = "") -> None:
        super().__init__(detail or failure.value)
        self.failure = failure
        self.detail = detail



class DurableOutputIdentityError(RuntimeError):
    """A terminal durable row names no verifiable commit identity."""

@dataclass
class SchedulerDeps:
    """Everything the scheduler needs from outside itself."""

    store: "lc.LifecycleStore"
    repo: Path
    integration_path: Path
    integration_branch: str
    worktrees_root: Path
    scratch_root: Path
    run_node: NodeRunner
    run_gate: GateRunner
    run_integration_gate: IntegrationGateRunner
    quiesce_attempt: QuiesceAttempt
    #: §10.2's threshold for the ONE integration gate this run ends with
    #: (§8.8), taken from `merge_policy.integration_gate.min_cases`.
    #:
    #: It replaces a `min_cases` scalar that all three adjudication sites
    #: read — both node gates and this one. That scalar could not be right:
    #: a run has many node gates, each declaring its own threshold, so one
    #: number per run structurally cannot carry them. It also had no
    #: production writer, so every gate in every run was adjudicated at its
    #: default of 1 while plans declared 5, 6, 8 and 70. A node's threshold
    #: now travels on the node (`PlanNode.gate_min_cases`); this one stays a
    #: scalar because there is exactly one integration gate per run, which is
    #: the case the old field was accidentally right about.
    integration_min_cases: int = 1
    #: `(attempt) -> None`. The watchdog performs the kill the worker cannot,
    #: because the worker thread is blocked reading the agent (§7.6). Killing
    #: an agent's process belongs to the launcher (§9.3, step 7), so it is
    #: supplied rather than implemented here. Left `None`, the watchdog still
    #: detects and fails a stalled attempt; it simply cannot terminate it, and
    #: that is a stated limit rather than a silent one.
    kill_attempt: Optional[Callable[..., None]] = None
    #: Provision runs after worktree creation and before pre-gate/baseline.
    provision: Optional[Callable[[Path], None]] = None
    #: The code-review stage (§7.3's review-node predicate). Runs after an
    #: attempt's own verification passes and before `mark_verified`, so a diff
    #: no model has read cannot reach the merge frontier.
    #:
    #: Optional, and its default is the honest one: left `None` the scheduler
    #: merges on gates alone, exactly as it did before this stage existed. That
    #: is a stated limit rather than a silent one — `maestro run start` always
    #: supplies it, and the golden offline scenario supplies a fake, so the
    #: unreviewed path is reachable only by a caller that constructs
    #: `SchedulerDeps` itself and omits it.
    review_attempt: Optional[ReviewRunner] = None

    def __post_init__(self) -> None:
        if self.quiesce_attempt is None:
            raise QuiescenceDependencyError(
                "quiesce_attempt is required: a scheduler cannot classify or "
                "retry while owned execution may still exist")


@dataclass
class _AttemptContext:
    record: Optional[st.AttemptRecord] = None
    settled: bool = False


@dataclass
class AcceptanceResult:
    """§8.8's final acceptance, as one reportable fact."""

    green: bool
    specs: Tuple[str, ...]
    gate: Optional["wt.GateResult"] = None
    ancestry: Dict[str, bool] = field(default_factory=dict)
    reason: str = ""


@dataclass
class RunReport:
    """What the scheduler declares when the run stops (§7.3)."""

    outcome: st.RunOutcome
    merged: Tuple[str, ...] = ()
    blocked: Tuple[Tuple[str, st.BlockReason], ...] = ()
    abandoned: Tuple[str, ...] = ()
    upstream_blocked: Tuple[str, ...] = ()
    acceptance: Optional[AcceptanceResult] = None
    ancestry: Dict[str, bool] = field(default_factory=dict)
    #: `node_id -> findings` for every node blocked `REVIEW_BUDGET_EXHAUSTED`.
    #: Reported beside the blocked set for the same reason the acceptance
    #: result is: a block reason names the rule that fired, never the thing to
    #: fix, and a lane that spent three attempts on review deserves to say what
    #: the reviewer objected to without the operator re-running it.
    review_findings: Dict[str, str] = field(default_factory=dict)

    @property
    def integration_untested(self) -> bool:
        """§8.8 — a BLOCKED run's branch is integration-untested, and
        `run status` says so rather than leaving it to be inferred from the
        absence of a gate result."""
        return self.acceptance is None


class Scheduler:
    """One run. Construct, `run()`, read the report.

    Not reusable across runs on purpose: a scheduler holds the run's thread
    pool, its cancellation flag, and its per-node output SHAs, and reusing
    one would be the overloaded role §4's RC4 convicts.
    """

    def __init__(self, run_id: str, nodes: Sequence[st.PlanNode],
                 config: st.SchedulerConfig, deps: SchedulerDeps,
                 plan_digest: str = "") -> None:
        self.run_id = run_id
        self.nodes: Dict[str, st.PlanNode] = {n.node_id: n for n in nodes}
        self.config = config
        self.deps = deps
        self.plan_digest = plan_digest

        self._cancelled = threading.Event()
        self._lock = threading.RLock()
        # A watchdog timeout revokes a generation before quiescence can
        # complete and release its retry. The durable row remains RUNNING
        # during that proof, so its state alone cannot stop a provisioner
        # unblocked by the watchdog from entering a gate or runner.
        self._watchdog_fences: Dict[Tuple[str, str, int], None] = {}
        self._output_shas: Dict[str, str] = {}
        # Latest typed guidance per acceptance surface, per node. Replaced a
        # plain last-failure string: verification and review took turns
        # overwriting one slot, and each attempt fixed exactly what the last
        # prompt named while regressing the constraint the prompt no longer
        # mentioned (run-32b19abadf4a4d6b801ae0f4456976c7 BLOCKED after five
        # such attempts). Rendered into the retry prompt at dispatch and read
        # nowhere else — nothing transitions on it (§10.1/§1.2).
        self._guidance: Dict[str, rp.GuidanceLedger] = {}
        #: Findings from the review that exhausted a node's budget, kept so the
        #: run report can surface *why* rather than only that a count ran out.
        self._review_findings: Dict[str, str] = {}
        self._pool: Optional[ThreadPoolExecutor] = None
        self._projected = False
        self._stuck = False
        _refuse_colliding_integration_branch(deps.integration_branch, run_id)

    # ── lifecycle of the scheduler itself ───────────────────────────────────

    def project(self) -> None:
        """Write the DAG projection, once (§7.1)."""
        if self._projected:
            return
        try:
            self.deps.store.create_run(self.run_id, self.plan_digest,
                                       list(self.nodes.values()))
        except lc.RunAlreadyExists:
            pass
        # Both durable states carry authority only after their persisted SHA is
        # revalidated. A crash after VERIFIED has no in-memory output map, so
        # excluding it would strand a ready merge forever; trusting an
        # unverified string would let a corrupt row choose the merge input.
        for node_id in self.nodes:
            row = self.deps.store.get_node(self.run_id, node_id)
            if row.state not in (st.NodeState.VERIFIED, st.NodeState.MERGED):
                continue
            output_sha = row.output_sha
            valid = output_sha is not None
            if valid and row.state is st.NodeState.VERIFIED:
                try:
                    attempt = self.deps.store.get_attempt(
                        self.run_id, node_id, row.attempt_no)
                except lc.UnknownNode:
                    valid = False
                else:
                    valid = (attempt.state is st.NodeState.VERIFIED
                             and wt.is_attempt_output_commit(
                                 Path(self.deps.repo), output_sha,
                                 run_id=self.run_id, node_id=node_id,
                                 attempt_no=row.attempt_no,
                                 expected_base=attempt.base_sha))
            elif valid:
                valid = (
                    wt.is_valid_output_commit(
                        Path(self.deps.integration_path), output_sha)
                    and wt.final_ancestry_sweep(
                        Path(self.deps.integration_path),
                        {node_id: output_sha}).get(node_id, False))
            if not valid:
                if row.state is st.NodeState.VERIFIED:
                    self.deps.store.mark_blocked(
                        self.run_id, node_id, st.BlockReason.OUTPUT_IDENTITY_INVALID)
                    continue
                raise DurableOutputIdentityError(
                    f"{self.run_id}/{node_id}: MERGED output identity is invalid")
            self._output_shas[node_id] = output_sha
        self._projected = True

    def cancel(self) -> None:
        """Latch cancellation; the run loop owns its quiescent completion."""
        with self._lock:
            self._cancelled.set()

    def shutdown(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)

    def _owns_running(self, record: st.AttemptRecord) -> bool:
        lifecycle = self.deps.store.get_node(self.run_id, record.node_id)
        return (lifecycle.state is st.NodeState.RUNNING
                and lifecycle.attempt_no == record.attempt_no)

    def _fence_watchdog_generation(self, record: st.AttemptRecord) -> None:
        """Revoke a timed-out generation before its quiescence proof blocks."""
        with self._lock:
            self._watchdog_fences[record.key] = None

    def _require_running(self, record: st.AttemptRecord) -> None:
        with self._lock:
            if self._cancelled.is_set():
                raise AttemptCancelled()
            if record.key in self._watchdog_fences:
                raise AttemptOwnershipLost(
                    f"{record.node_id}#{record.attempt_no} was fenced by watchdog")
            if not self._owns_running(record):
                raise AttemptOwnershipLost(
                    f"{record.node_id}#{record.attempt_no} no longer owns RUNNING")

    def _quiesce(self, record: st.AttemptRecord, phase: str) -> None:
        try:
            self.deps.quiesce_attempt(record, phase)
        except BaseException as exc:
            raise QuiescenceFailure(phase, exc) from exc

    def _settle_context(self, context: _AttemptContext) -> None:
        if context.record is not None and not context.settled:
            self._quiesce(context.record, "settle")
            context.settled = True

    def _block_quiescence(self, node: st.PlanNode,
                          record: Optional[st.AttemptRecord],
                          failure: QuiescenceFailure) -> None:
        if record is None:
            return
        with self._lock:
            if not self._owns_running(record):
                return
            cause = failure.__cause__
            # The type alone names the guard that fired, never the condition it
            # fired on, and the chain below it is where the actual failure is:
            # a quiescence error raised from another quiescence error reports
            # only its own name at every level. Record the messages so a blocked
            # node can be diagnosed from the row instead of by re-running.
            chain = []
            seen = set()
            current: Optional[BaseException] = cause
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                chain.append("{0}: {1}".format(
                    type(current).__name__, current))
                current = current.__cause__ or current.__context__
            self.deps.store.mark_blocked(
                self.run_id, node.node_id, st.BlockReason.QUIESCENCE_UNPROVEN,
                detail={"phase": failure.phase,
                        "exception_type": type(cause).__name__,
                        "causes": chain[:6]})

    def _request_cancel(self, node_id: str) -> None:
        lifecycle = self.deps.store.get_node(self.run_id, node_id)
        if lifecycle.state is not st.NodeState.RUNNING:
            return
        try:
            record = self.deps.store.get_attempt(
                self.run_id, node_id, lifecycle.attempt_no)
        except lc.UnknownNode:
            return
        try:
            self._quiesce(record, "cancel")
        except QuiescenceFailure as exc:
            node = self.nodes.get(node_id)
            if node is not None:
                self._block_quiescence(node, record, exc)

    # ── the main loop ───────────────────────────────────────────────────────

    def run(self) -> RunReport:
        """Schedule until quiescence, then declare exactly one outcome.

        Quiescence is "nothing in flight and no node can progress" — not "no
        pending nodes", because a node stranded behind a blocked ancestor
        stays PENDING forever and is exactly the shape the run must stop on.
        """
        self.project()
        if self._cancelled.is_set():
            self.deps.store.cancel_run(self.run_id)
            return self._declare()

        self._pool = ThreadPoolExecutor(max_workers=self.config.concurrency)
        in_flight: Dict[str, "Future"] = {}
        cancellation_requested = set()
        watchdog, backstop = self._start_liveness()
        watchdog.start()
        try:
            while True:
                if self._cancelled.is_set():
                    # A Future cannot interrupt a running worker. Quiesce its
                    # owned execution first, then wait for the worker to stop
                    # observing its RUNNING lease before cancelling durable
                    # state. Otherwise a late worker could commit into a retry.
                    for node_id, future in list(in_flight.items()):
                        future.cancel()
                        if node_id not in cancellation_requested:
                            self._request_cancel(node_id)
                            cancellation_requested.add(node_id)
                    if in_flight:
                        done, _ = _wait_any(list(in_flight.items()))
                        for node_id in done:
                            in_flight.pop(node_id, None)
                        continue
                    self.deps.store.cancel_run(self.run_id)
                    break

                if backstop.check():
                    # §11.2 — declared with work still in flight, deliberately:
                    # the backstop's domain is the run's stopping point, not
                    # quiescence, because both hang shapes it exists for have
                    # something in flight and nothing transitioning.
                    self._stuck = True
                    for future in list(in_flight.values()):
                        future.cancel()
                    break

                self._merge_frontier()
                if self._cancelled.is_set():
                    continue

                ready = [node_id for node_id in self.deps.store.ready_nodes(self.run_id)
                         if node_id not in in_flight]
                for node_id in ready:
                    if (self._cancelled.is_set()
                            or len(in_flight) >= self.config.concurrency):
                        break
                    in_flight[node_id] = self._pool.submit(self._attempt, node_id)

                if not in_flight:
                    # Nothing running. If the frontier produced nothing to
                    # start either, no node can progress and the run is
                    # quiescent.
                    self._merge_frontier()
                    if not self.deps.store.ready_nodes(self.run_id):
                        break
                    continue

                done, _ = _wait_any(list(in_flight.items()))
                for node_id in done:
                    in_flight.pop(node_id, None)
        finally:
            watchdog.stop()
            self.shutdown()

        return self._declare()

    def _start_liveness(self):
        """Start §7.6's single watchdog thread and §11.2's run-level timer.

        Two clocks have to be made to agree here, and getting it wrong is
        silent in the worst direction. `RunBackstop` defaults its time source
        to `time.monotonic`, while the store's `last_transition_at` returns
        epoch seconds — subtracting one from the other yields a number with no
        meaning, and on this machine a large negative one, so the backstop
        would simply never fire. The timer is therefore given `time.time`,
        matching the column it reads.
        """
        store = self.deps.store

        def running_attempts():
            return [a for a in store.attempts_for(self.run_id)
                    if a.state is st.NodeState.RUNNING]

        def kill(attempt):
            # `_stall` calls the killer before `fail_attempt`. Revoke the
            # worker's authority here, rather than after the potentially
            # blocking quiescence proof in `fail`, so no resumed provisioner
            # can pass its next generation boundary in that interval.
            self._fence_watchdog_generation(attempt)
            if self.deps.kill_attempt is None:
                return
            try:
                self.deps.kill_attempt(attempt)
            except BaseException as exc:
                node = self.nodes.get(attempt.node_id)
                if node is not None:
                    self._block_quiescence(
                        node, attempt, QuiescenceFailure("watchdog-kill", exc))

        def fail(attempt, retry_class, reason):
            # `reason` is the watchdog's `StallReason` value -- NODE_TIMEOUT,
            # PROCESS_DEAD, or TURN_TIMEOUT, the typed answer to "which of the
            # three signals convicted this attempt" (§7.6). It was accepted
            # here and dropped, so every watchdog-driven ENVIRONMENTAL retry
            # and every ENVIRONMENTAL_BUDGET_EXHAUSTED block reached the ledger
            # with `detail_json == {}` and an operator learned the class and
            # nothing else. It is carried into the classification below.
            #
            # The watchdog's kill request is not proof: only the mandatory
            # quiescer can establish group absence before this generation is
            # released for retry. Fence again because this callback is also
            # the authority boundary if a Watchdog implementation reaches it
            # without calling `kill`.
            self._fence_watchdog_generation(attempt)
            node = self.nodes.get(attempt.node_id)
            if node is None:
                return
            try:
                self._quiesce(attempt, "watchdog")
            except QuiescenceFailure as exc:
                self._block_quiescence(node, attempt, exc)
                return
            if not self._cancelled.is_set():
                self._settle_failure(
                    node,
                    rp.Classification(retry_class=retry_class, reason=reason),
                    record=attempt, allow_watchdog_fence=True)

        watchdog = wd.Watchdog(
            config=self.config,
            attempts_provider=running_attempts,
            write_heartbeat=store.record_heartbeat,
            kill=kill,
            fail_attempt=fail,
            time_source=time.time)

        backstop = wd.RunBackstop(
            config=self.config,
            last_transition_at=lambda: store.last_transition_at(self.run_id),
            on_stuck=lambda diagnostic: None,
            diagnostic=self.status_diagnostic,
            time_source=time.time)
        return watchdog, backstop

    def status_diagnostic(self) -> str:
        """The "why is nothing happening" text §11.2 requires the scheduler to
        print rather than exiting silently — the same answer `run status`
        gives, so an operator never has to read the database by hand."""
        store = self.deps.store
        lines = [f"run {self.run_id}: no lifecycle transition within "
                 f"T={self.config.backstop_t_s}s"]
        stranded = set(store.upstream_blocked(self.run_id))
        for record in sorted(store.node_records(self.run_id),
                             key=lambda r: (r.depth, r.node_id)):
            why = ""
            if record.node_id in stranded:
                why = " (an ancestor is blocked or abandoned)"
            elif record.state == st.NodeState.PENDING.value:
                unmet = [d for d in record.needs
                         if not _is_merged(store, self.run_id, d)]
                if unmet:
                    why = f" (waiting on {', '.join(sorted(unmet))})"
            lines.append(f"  {record.node_id}: {record.state}{why}")
        # §7.8 — panes a resumed process could not reach are recorded in
        # `orphans` and reported here. The scheduler never adopts them and
        # never kills them, so if this text does not name them the stated cost
        # of resume ("visible and killed by hand") has no visible half.
        orphans = store.audit_orphans(self.run_id)
        if orphans:
            lines.append(f"  {len(orphans)} orphaned pane(s) — kill by hand:")
            for row in orphans:
                where = row.get("handle") or (
                    f"pid {row['pid']}" if row.get("pid") is not None else "no handle recorded")
                lines.append(f"    orphan {row.get('node_id', '?')}"
                             f"#{row.get('attempt_no', '?')}: {where}")
        return "\n".join(lines)

    # ── one attempt (§7.3, §7.4, §8.3, §8.4) ────────────────────────────────

    def _attempt(self, node_id: str) -> None:
        """Run one leased attempt and never classify a superseded generation."""
        node = self.nodes[node_id]
        context = _AttemptContext()
        try:
            self._attempt_body(node, context)
        except (AttemptCancelled, AttemptOwnershipLost):
            # Even a superseded/cancelled worker must prove its latest boundary
            # quiescent before returning control to the scheduler loop.
            try:
                self._settle_context(context)
            except QuiescenceFailure as exc:
                self._block_quiescence(node, context.record, exc)
            return
        except QuiescenceFailure as exc:
            self._block_quiescence(node, context.record, exc)
        except wt.HarnessQuiescenceError as exc:
            self._block_quiescence(
                node, context.record, QuiescenceFailure("harness-gate", exc))
        except BaseException as exc:  # noqa: BLE001 — containment is the point
            try:
                self._settle_context(context)
            except QuiescenceFailure as quiescence:
                self._block_quiescence(node, context.record, quiescence)
                return
            if (context.record is not None
                    and not self._cancelled.is_set()
                    and self._owns_running(context.record)):
                # A `LaunchFailed` names its own class in a typed enum member,
                # so `classify`'s launcher branch is reachable from the real
                # adapter for the first time (§16.3 item 42). Every other
                # exception still falls to the ENVIRONMENTAL default,
                # unchanged and fail-closed.
                signal = rp.FailureSignal(
                    node_kind=node.kind, exception_type=type(exc).__name__,
                    launcher_failure=(exc.failure
                                      if isinstance(exc, LaunchFailed)
                                      else None))
                # `classify` reads none of the signal's ENVIRONMENTAL evidence
                # by design (§7.5 forbids the lexical shortcut), so its
                # fall-through returns a classification with no account of
                # itself and the row lands empty. The exception is what this
                # arm observed, and the launcher's typed vocabulary --
                # LAUNCH_REFUSED, AGENT_GONE, ENVELOPE_UNPARSED -- travels in
                # it. Recorded the way `_block_quiescence` records a cause:
                # type and message, written and never read back (§10.1).
                self._settle_failure(
                    node, _with_reason(rp.classify(signal),
                                       _exception_reason(exc)),
                    record=context.record)

    def _attempt_body(self, node: st.PlanNode, context: _AttemptContext) -> None:
        store = self.deps.store
        head = wt.integration_head(self.deps.repo, self.deps.integration_branch)

        # §7.6 — the window opens BEFORE the worktree exists, so a hung
        # `git worktree add` is inside it rather than outside.
        attempt_no = store.start_attempt(self.run_id, node.node_id, head)
        record = store.get_attempt(self.run_id, node.node_id, attempt_no)
        context.record = record
        self._require_running(record)

        attempt = wt.create_attempt_worktree(
            self.deps.repo, self.run_id, node.node_id, attempt_no, head,
            Path(self.deps.worktrees_root), Path(self.deps.scratch_root))
        self._require_running(record)

        created = wt.check_at_create(attempt)
        self._require_running(record)
        if not created.ok:
            self._settle_context(context)
            # `CheckResult.detail` already names which of §8.3's checks failed
            # and on what; discarding it here left the third ENVIRONMENTAL arm
            # writing the same empty row as the other two.
            self._settle_failure(
                node, rp.Classification(
                    retry_class=st.RetryClass.ENVIRONMENTAL,
                    reason=_check_result_reason(created)),
                record=record)
            return

        pre_verdict = None
        try:
            if self.deps.provision is not None:
                self.deps.provision(attempt.path)
            # A slow provision may finish after a watchdog revoked this
            # generation. Its return is a fence: no stale worker reaches a
            # gate, runner, inventory, or commit.
            self._require_running(record)
            if node.kind is st.NodeKind.AGENT:
                self._require_running(record)
                pre = self.deps.run_gate(
                    attempt, node, "pre", self._cancelled.is_set)
                # A selector this node is declared to produce, absent at its
                # base, is the red clause 2 asks for -- not a broken runner.
                selector = (node.gate_selector or "").strip()
                unbuilt = bool(
                    selector and selector in node.outputs
                    and not (attempt.path / selector).exists())
                pre_verdict = vf.adjudicate_pre_gate(
                    pre, node.gate_min_cases, selector_unbuilt=unbuilt)
        finally:
            # Provision and the pre gate both execute before the measurement
            # bracket; their process groups must be absent before its baseline.
            self._quiesce(record, "pre-baseline")
        self._require_running(record)

        if pre_verdict is not None and pre_verdict.green:
            self._settle_context(context)
            self._settle_failure(
                node, rp.Classification(
                    block_reason=st.BlockReason.GATE_NOT_FALSIFIABLE),
                record=record)
            return

        baseline = wt.take_baseline(attempt)
        self._require_running(record)

        def on_launch(pid: Optional[int] = None) -> None:
            """Arm liveness only while this exact generation still owns RUNNING."""
            with self._lock:
                self._require_running(record)
                store.mark_launched(self.run_id, node.node_id, attempt_no, pid)

        self._require_running(record)
        try:
            execution = self.deps.run_node(
                attempt, node, record,
                rp.render_guidance(node, self._guidance.get(node.node_id)),
                on_launch, self._cancelled.is_set)
        finally:
            # This runs even if the runner raises; classification below cannot
            # release or block the attempt until its owned group is absent.
            self._quiesce(record, "pre-inventory")
        self._require_running(record)
        if execution.launched_pid is not None:
            # A runner that reports its pid only on return still arms the
            # signals, just late — recorded so the row is complete either way.
            with self._lock:
                self._require_running(record)
                store.mark_launched(self.run_id, node.node_id, attempt_no,
                                    execution.launched_pid)

        after = wt.inventory(attempt.path)
        self._require_running(record)
        measured = wt.delta(baseline, after)
        permission = wt.permission_check(attempt, measured, node.outputs)

        if node.kind is st.NodeKind.CODE:
            verdict = vf.verify_code_node(
                exit_code=execution.exit_code, permission=permission,
                diff_empty=not measured.touched,
                expects_changes=node.expects_changes)
        else:
            # Clause 4 is evaluated here, at measurement, and the commit
            # follows it immediately (§8.4). Clause 3 is evaluated after the
            # commit, against the committed tree, so it is passed in below
            # only once the commit has been taken.
            verdict = vf.verify_agent_node(
                envelope_parsed=execution.envelope_parsed,
                pre_gate=pre_verdict, post_gate=_pending_gate(),
                permission=permission)

        if not verdict.verified and verdict.failed_clause != 3:
            self._settle_context(context)
            self._settle_verdict(node, verdict, execution, record)
            return

        with self._lock:
            self._require_running(record)
            output_sha = wt.commit_measured_delta(
                attempt, measured, after,
                f"{node.node_id} attempt {attempt_no}")
        self._require_running(record)
        wt.check_post_commit(
            attempt, wt.expected_inventory(baseline, measured, after))
        self._require_running(record)

        if node.kind is st.NodeKind.AGENT:
            self._require_running(record)
            post = self.deps.run_gate(
                attempt, node, "post", self._cancelled.is_set)
            self._require_running(record)
            verdict = vf.verify_agent_node(
                envelope_parsed=execution.envelope_parsed,
                pre_gate=pre_verdict,
                post_gate=vf.adjudicate_gate(post, node.gate_min_cases),
                permission=permission)
            if not verdict.verified:
                self._settle_context(context)
                self._settle_verdict(node, verdict, execution, record)
                return

        # The final proof covers post-gates as well as every failure path
        # above. Only then may the scheduler make a durable state transition.
        self._settle_context(context)

        # ── the review gate (§7.3's review-node predicate) ──────────────────
        #
        # Here, and not one line earlier or later. After `_settle_context`,
        # because that is the proof this attempt's owned process group is
        # absent, and the reviewer opens a pane of its own — starting it while
        # the builder might still be alive would put two owned groups inside
        # one attempt's quiescence obligation. Before `mark_verified`, because
        # VERIFIED is what `_merge_frontier` reads: a node that reaches it has
        # already won the right to merge, and a gate after that point would be
        # judging code the scheduler had already committed to.
        if self.deps.review_attempt is not None:
            self._require_running(record)
            try:
                review = self.deps.review_attempt(
                    attempt, node, record, head, output_sha)
            except cr.ReviewStalled:
                # A reviewer that never reported says nothing about the code.
                # ENVIRONMENTAL, so it spends an infra retry rather than a
                # review attempt, and mutates no prompt — telling a builder to
                # fix findings that were never produced would be the refund
                # loop §7.5 convicts.
                self._settle_failure(
                    node, rp.Classification(
                        retry_class=st.RetryClass.ENVIRONMENTAL,
                        reason="the code reviewer stalled without reporting"),
                    record=record)
                return
            self._require_running(record)
            if not review.passed:
                self._settle_review_rejection(node, review, record)
                return

        with self._lock:
            self._require_running(record)
            self._output_shas[node.node_id] = output_sha
            store.mark_verified(self.run_id, node.node_id, output_sha)

    # ── settling a failed attempt ───────────────────────────────────────────

    def _settle_verdict(self, node: st.PlanNode, verdict: "vf.VerificationVerdict",
                        execution: NodeExecution, record: st.AttemptRecord) -> None:
        """Turn a failed VERIFIED predicate into a classification (§7.5)."""
        if verdict.block_reason is not None:
            self._settle_failure(
                node, rp.Classification(block_reason=verdict.block_reason),
                record=record)
            return

        signal = rp.FailureSignal(
            node_kind=node.kind,
            exit_code=execution.exit_code,
            gate=rp.GateOutcome(pre_gate_failed=True, post_gate_passed=False)
            if verdict.failed_clause == 3 else None,
            report=rp.ReportOutcome(parsed=execution.envelope_parsed,
                                    failed=verdict.failed_clause == 3),
            launcher_failure=execution.launcher_failure)
        classification = (rp.Classification(retry_class=verdict.retry_class)
                          if verdict.retry_class is not None
                          else rp.classify(signal))
        # The adapter's own account of the execution, attached where every
        # other observation is attached. `_with_reason` never overwrites, and
        # neither arm above sets one, so this is the ledger's only chance to
        # record what the launcher saw (§7.6).
        self._settle_failure(
            node, _with_reason(classification, execution.launch_detail or None),
            verdict, record)

    def _settle_failure(self, node: st.PlanNode, classification: rp.Classification,
                        verdict: Optional["vf.VerificationVerdict"] = None,
                        record: Optional[st.AttemptRecord] = None,
                        allow_watchdog_fence: bool = False) -> None:
        """Block or release only the generation that still owns RUNNING."""
        if record is None:
            return
        store = self.deps.store
        with self._lock:
            if (self._cancelled.is_set()
                    or not self._owns_running(record)
                    or (record.key in self._watchdog_fences
                        and not allow_watchdog_fence)):
                return
            # Why the attempt failed, computed once and written wherever this
            # failure is recorded — the retry row below and, above all, the
            # block rows here. A block is terminal, so its transition is the
            # last chance the ledger has to say what failed.
            detail = _failure_detail(classification, verdict)

            if classification.block_reason is not None:
                store.mark_blocked(
                    self.run_id, node.node_id, classification.block_reason,
                    detail=detail or None)
                return

            retry_class = classification.retry_class or st.DEFAULT_RETRY_CLASS
            if retry_class is st.RetryClass.SEMANTIC:
                lifecycle = store.get_node(self.run_id, node.node_id)
                if self._semantic_ceiling_reached(
                        node.node_id, lifecycle.granted_extra_attempts):
                    store.mark_blocked(
                        self.run_id, node.node_id,
                        st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
                        detail=detail or None, retry_class=retry_class)
                    return
                # Only SEMANTIC mutates the prompt, and the offending paths are
                # named in it — which is what makes the retry genuinely new
                # instructions rather than the same request repeated (§7.5).
                # The entry consumes the same typed `detail` record the store
                # rows get — one representation of the failure, not two — and
                # replaces only this surface's slot: the reviewer's standing
                # findings, if any, survive into the next prompt.
                self._guidance[node.node_id] = self._guidance.get(
                    node.node_id, rp.GuidanceLedger()).with_verification(
                        rp.verification_guidance(detail))
            else:
                # LAUNCHER_TRANSIENT's budget is a property of the member, not
                # the class: §7.5 gives CREDENTIAL zero and the rest one or
                # two. `config.retry_budget` cannot express that, so asking it
                # alone gave a credential refusal the same two retries as a
                # dropped transport — the zero-retry rule stated in §7.5 and
                # implemented in `launcher_retry_budget` had no caller.
                budget = (rp.launcher_retry_budget(
                              self.config, classification.launcher_failure)
                          if retry_class is st.RetryClass.LAUNCHER_TRANSIENT
                          else self.config.retry_budget(retry_class))
                spent = store.attempts_spent(
                    self.run_id, node.node_id, retry_class)
                if spent >= budget:
                    store.mark_blocked(
                        self.run_id, node.node_id,
                        _budget_reason(retry_class,
                                       classification.launcher_failure),
                        detail=detail or None, retry_class=retry_class)
                    return

            store.fail_attempt(self.run_id, node.node_id, retry_class,
                               detail=detail or None)

    def _settle_review_rejection(self, node: st.PlanNode, review: Any,
                                 record: st.AttemptRecord) -> None:
        """Recycle the attempt with the reviewer's findings, or block on budget.

        The same shape as `_settle_failure`'s SEMANTIC arm, and deliberately so
        — a rejected diff earns another attempt against *new instructions*,
        never a repeat of the same request. What differs is the budget: this
        counts against `review_ceiling` through a marker on the attempt row,
        and the semantic ceiling is untouched. A node whose gate went red twice
        still gets its full review allowance, and vice versa.
        """
        store = self.deps.store
        findings = review.findings_text()
        with self._lock:
            if (self._cancelled.is_set()
                    or not self._owns_running(record)
                    or record.key in self._watchdog_fences):
                return

            marker = {rp.REVIEW_REJECTED_KEY: True,
                      "review_subject_digest": review.subject_digest}
            detail = {
                "reason": "code review rejected the diff",
                "subject_digest": review.subject_digest,
                "replayed": bool(review.replayed),
                # The check ids only. The messages are the builder's retry
                # guidance and live in the prompt; duplicating their prose into
                # a durable audit row would be the second representation §4
                # convicts, and §10.1 forbids any guard reading it back.
                "blocking_checks": [c.check_id for c in review.findings],
            }

            lifecycle = store.get_node(self.run_id, node.node_id)
            if self._review_ceiling_reached(
                    node.node_id, lifecycle.granted_extra_attempts):
                store.mark_blocked(
                    self.run_id, node.node_id,
                    st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                    detail=detail, attempt_extra=marker)
                # Surfaced where the operator will actually read it: the block
                # reason alone says a budget ran out and nothing about what the
                # reviewer objected to, which is the evidence gap that makes a
                # blocked node undiagnosable without re-running it.
                self._review_findings[node.node_id] = findings
                return

            # Replaces only the REVIEW slot: a rejection must not erase what
            # verification said, or the node oscillates between the two
            # surfaces fixing one constraint while regressing the other.
            self._guidance[node.node_id] = self._guidance.get(
                node.node_id, rp.GuidanceLedger()).with_review(
                    rp.review_guidance(review))
            store.fail_attempt(self.run_id, node.node_id,
                               st.RetryClass.SEMANTIC,
                               detail=detail, attempt_extra=marker)

    def _review_ceiling_reached(self, node_id: str, granted: int) -> bool:
        """The review ceiling, counting the attempt that is failing right now.

        The same off-by-one `_semantic_ceiling_reached` documents: the policy
        counts marker rows that already exist, and this attempt's marker is
        written by the call this decision gates. Without the increment K would
        admit K+1 attempts.
        """
        attempts = self.deps.store.attempts_for(self.run_id, node_id)
        already = rp.review_attempts_total(attempts, node_id)
        return already + 1 >= self.config.review_ceiling + granted

    def _semantic_ceiling_reached(self, node_id: str, granted: int) -> bool:
        """§7.5's ceiling, counting the attempt that is failing right now.

        The retry policy counts SEMANTIC rows that already exist, and the
        attempt currently failing is not one of them yet — its class is
        written by `fail_attempt`, which is the call this decision gates. So
        the in-flight attempt is added here rather than in the policy: the
        policy owns the rule, and the scheduler owns knowing that one more
        attempt has just been spent.

        Without the increment, K would admit K+1 attempts, which is the
        off-by-one that turns a stated ceiling into a slightly higher unstated
        one.
        """
        attempts = self.deps.store.attempts_for(self.run_id, node_id)
        already = rp.semantic_attempts_total(attempts, node_id)
        return already + 1 >= self.config.semantic_ceiling + granted

    # ── §8.5 the merge, serialized on the loop thread ───────────────────────

    def _merge_frontier(self) -> None:
        """Merge every frontier node that is VERIFIED, in order.

        Serialized deliberately. §8.5's order is a function of the graph, the
        merged set, and the blocked set only, so merging inside whichever
        worker happened to finish would make the sequence depend on timing —
        the property the frontier exists to remove.
        """
        while not self._cancelled.is_set():
            records = self.deps.store.node_records(self.run_id)
            candidate = wt.merge_ready(records)
            if candidate is None:
                return
            with self._lock:
                if self._cancelled.is_set():
                    return
                output_sha = self._output_shas.get(candidate.node_id)
            if output_sha is None:
                return

            result = wt.merge_verified_node(Path(self.deps.integration_path),
                                            candidate.node_id, output_sha)
            if result.conflicted_paths:
                # §8.7 — capture, abort, block with the evidence, and let the
                # descendants become derived-unready. Resolution is human: a
                # conflict means two output sets overlapped in content though
                # their declared globs did not, which is a planning defect
                # that re-prompting papers over.
                self.deps.store.mark_blocked(
                    self.run_id, candidate.node_id, st.BlockReason.MERGE_CONFLICT,
                    detail={"conflicted_paths": list(result.conflicted_paths)})
                continue
            if not result.ancestry_proven:
                self.deps.store.mark_blocked(
                    self.run_id, candidate.node_id, st.BlockReason.MERGE_CONFLICT,
                    detail={"reason": "ancestry not proven after merge"})
                continue
            if self._cancelled.is_set():
                return
            self.deps.store.mark_merged(self.run_id, candidate.node_id)

    # ── §8.8 final acceptance, and §7.3's declaration ───────────────────────

    def _declare(self) -> RunReport:
        store = self.deps.store
        # The Event is not authority. Persist it before either accepting a
        # candidate or declaring the terminal outcome, including cancellation
        # that arrived while final acceptance was running.
        if self._cancelled.is_set():
            store.cancel_run(self.run_id)

        records = store.node_records(self.run_id)
        states = {r.node_id: r.state for r in records}
        merged = tuple(sorted(n for n, s in states.items()
                              if s == st.NodeState.MERGED.value))

        acceptance = None
        if not self._cancelled.is_set() and self._is_candidate_accepted(states):
            acceptance = self._run_final_acceptance(records, merged)
        with self._lock:
            if self._cancelled.is_set():
                store.cancel_run(self.run_id)
                records = store.node_records(self.run_id)
                states = {r.node_id: r.state for r in records}
                merged = tuple(sorted(n for n, s in states.items()
                                      if s == st.NodeState.MERGED.value))
            cancelled = tuple(sorted(n for n, s in states.items()
                                     if s == st.NodeState.CANCELLED.value))
            blocked = tuple(sorted(
                (n, store.get_node(self.run_id, n).block_reason)
                for n, s in states.items() if s == st.NodeState.BLOCKED.value))
            # This closes the cancellation/ACCEPTED race: after the final gate
            # returned, either cancellation is made durable here or acceptance
            # is declared before a later cancellation request can take effect.
            declared = store.declare_outcome(
                self.run_id, stuck=self._stuck,
                acceptance_result=(acceptance.green if acceptance else None))

        return RunReport(
            outcome=declared.outcome,
            merged=merged,
            blocked=blocked,
            abandoned=cancelled,
            upstream_blocked=store.upstream_blocked(self.run_id),
            acceptance=acceptance,
            ancestry=dict(acceptance.ancestry) if acceptance else {},
            review_findings={
                node_id: findings
                for node_id, findings in self._review_findings.items()
                if any(n == node_id for n, _ in blocked)})

    def _is_candidate_accepted(self, states: Mapping[str, str]) -> bool:
        """§8.8 — at least one node MERGED and every other MERGED or
        abandoned to CANCELLED. On every other quiescent shape there is no
        final head that represents the plan, so acceptance never runs."""
        values = set(states.values())
        return (st.NodeState.MERGED.value in values
                and values <= {st.NodeState.MERGED.value,
                               st.NodeState.CANCELLED.value})

    def _run_final_acceptance(self, records: Sequence["wt.NodeRecord"],
                              merged: Sequence[str]) -> AcceptanceResult:
        """The bounded window of §8.8, opened by the acceptance-start refresh.

        The refresh is not bookkeeping: nothing transitions between the last
        node's MERGED and the outcome declaration, and that gap is as long as
        everything acceptance executes — the ancestry sweep, the union of
        merged specs, and the integration gate. Without the refresh the
        run-level backstop measures that healthy gap as silence.
        """
        store = self.deps.store
        if self._cancelled.is_set():
            return AcceptanceResult(
                green=False, specs=(), reason="cancellation requested before acceptance")
        store.acceptance_started(self.run_id)
        deadline = time.monotonic() + self.config.final_acceptance_timeout_s

        with self._lock:
            shas = {n: self._output_shas[n] for n in merged if n in self._output_shas}
        ancestry = wt.final_ancestry_sweep(Path(self.deps.integration_path), shas)
        if self._cancelled.is_set():
            return AcceptanceResult(
                green=False, specs=(), ancestry=ancestry,
                reason="cancellation requested during the final ancestry sweep")
        if not all(ancestry.values()) or len(ancestry) != len(merged):
            return AcceptanceResult(
                green=False, specs=(), ancestry=ancestry,
                reason="the final ancestry sweep did not re-prove every merged node")
        if time.monotonic() > deadline:
            return AcceptanceResult(green=False, specs=(), ancestry=ancestry,
                                    reason="final-acceptance timeout during the sweep")

        specs = wt.acceptance_specs(records)
        try:
            gate = self.deps.run_integration_gate(
                Path(self.deps.integration_path), specs, self._cancelled.is_set)
        except wt.GateCancelled:
            return AcceptanceResult(
                green=False, specs=specs, ancestry=ancestry,
                reason="cancellation requested during the final integration gate")
        if self._cancelled.is_set():
            return AcceptanceResult(
                green=False, specs=specs, gate=gate, ancestry=ancestry,
                reason="cancellation requested during the final integration gate")
        verdict = vf.adjudicate_gate(gate, self.deps.integration_min_cases)
        if time.monotonic() > deadline:
            return AcceptanceResult(green=False, specs=specs, gate=gate,
                                    ancestry=ancestry,
                                    reason="final-acceptance timeout during the gate")
        return AcceptanceResult(green=verdict.green, specs=specs, gate=gate,
                                ancestry=ancestry, reason=verdict.reason)


class IntegrationBranchCollision(ValueError):
    """The integration branch would shadow the attempt branch namespace."""


def _refuse_colliding_integration_branch(integration_branch: str, run_id: str) -> None:
    """Refuse an integration branch that git cannot coexist with (§8.2).

    Attempt branches are `maestro/{run_id}/{node_id}/a{n}`, and git stores a
    ref as a *file* at `refs/heads/<name>`. So a branch literally named
    `maestro/{run_id}` occupies the path that every attempt branch of that run
    needs as a *directory*, and `git worktree add -b` fails with "cannot lock
    ref ... exists" on the very first node.

    The trap is that `maestro/{run_id}` is the obvious name to choose, and the
    failure it produces names a ref rather than the branch the operator
    picked. Refusing at construction is the difference between a stated
    convention and a mechanism — this document convicts the former everywhere
    else, and a run that dies on its first worktree is exactly the cost of
    leaving it a convention here.
    """
    prefix = f"maestro/{run_id}"
    if integration_branch == prefix or integration_branch.startswith(prefix + "/"):
        raise IntegrationBranchCollision(
            f"integration branch {integration_branch!r} collides with the attempt "
            f"branch namespace {prefix}/<node>/a<n>: git cannot hold a ref and a "
            f"ref directory at the same path, so every attempt worktree would "
            f"fail to create. Choose a name outside {prefix}/.")


# ── helpers ─────────────────────────────────────────────────────────────────

def _pending_gate() -> "vf.GateVerdict":
    """A stand-in for clause 3 before the commit exists.

    Green, because at this point in the order clause 3 has not been evaluated
    and must not be allowed to fail the conjunction early — the real
    evaluation happens against the committed tree a few lines later. Named
    rather than inlined so that no reader mistakes it for a gate result.
    """
    return vf.GateVerdict(green=True, unparseable=False, counts=None,
                          reason="clause 3 not yet evaluated")


def _is_merged(store, run_id: str, node_id: str) -> bool:
    return any(r.node_id == node_id and r.state == st.NodeState.MERGED.value
               for r in store.node_records(run_id))


def _budget_reason(retry_class: st.RetryClass,
                   launcher_failure: Optional[rp.LauncherFailure] = None
                   ) -> st.BlockReason:
    """The block a spent budget writes.

    CREDENTIAL is separated because §7.5 gives it a *zero* budget: it blocks
    on its first occurrence having spent no retry at all, so
    LAUNCHER_BUDGET_EXHAUSTED would tell an operator a budget ran out when
    none ever existed. `BlockReason.CREDENTIAL_REFUSED` had no production
    writer before this, though §11.3's escape table already carries a row for
    it — the reason existed, and nothing could produce it.

    The default keeps the watchdog's call site correct rather than merely
    compiling: a stall is never a launcher failure, so it passes no member and
    lands on the environmental arm exactly as it did before.
    """
    if retry_class is st.RetryClass.LAUNCHER_TRANSIENT:
        if launcher_failure is rp.LauncherFailure.CREDENTIAL:
            return st.BlockReason.CREDENTIAL_REFUSED
        return st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED
    return st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED


def _with_reason(classification: rp.Classification,
                 reason: Optional[str]) -> rp.Classification:
    """Give a classification an account of itself when it arrived without one.

    `classify` is structural, never lexical (§7.5), so its ENVIRONMENTAL
    fall-through deliberately reads none of the evidence that reached it and
    returns `reason=None`. That is correct for *classifying* and wrong for
    *recording*: the caller holds the observation, and this is where it is
    attached. An existing reason is never overwritten -- the classifier's own
    account outranks the call site's.
    """
    if not reason or classification.reason:
        return classification
    return replace(classification, reason=reason)


def _exception_reason(exc: BaseException) -> str:
    """Type and message, the shape `_block_quiescence` already records."""
    return "{0}: {1}".format(type(exc).__name__, exc)


def _check_result_reason(result: "wt.CheckResult") -> str:
    """Which §8.3 check failed, from the check's own typed detail."""
    if result.detail:
        return "{0} check failed: {1}".format(
            result.stage, "; ".join(result.detail))
    return "{0} check failed".format(result.stage)


def _failure_detail(classification: rp.Classification,
                    verdict: Optional["vf.VerificationVerdict"]
                    ) -> Dict[str, Any]:
    """Why one attempt failed, as a typed record (§1.1 item 4).

    A bare `retry:ENVIRONMENTAL` row says a retry was spent and nothing about
    what to fix. The same gap is worse on a block, because a block is
    terminal: an observed run blocked a node with `blocked:SEMANTIC_BUDGET_
    EXHAUSTED` and `detail_json == {}`, and the reason its last attempt failed
    existed nowhere in the ledger — it was recoverable only from the worktree's
    git history. The earlier attempts' reasons survived only because they had
    been rendered into the *next* attempt's prompt, which makes prose the
    carrier of the evidence and is exactly what §1.2 refuses.

    So this is computed once per failure and written to every row that records
    it. Typed fields only — the failed clause, the paths the attempt wrote
    outside its declared outputs, and the classifier's own reason. No guard
    reads any of them back; nothing here causes a transition (§10.1).
    """
    detail: Dict[str, Any] = {}
    reason = getattr(classification, "reason", None)
    if reason:
        detail["reason"] = reason
    if classification.launcher_failure is not None:
        # Typed, not prose: which of §7.5's four launcher triggers convicted
        # this attempt. `reason` above carries the adapter's own message for a
        # human to read; this is the field a later reader can count, and it is
        # what tells CREDENTIAL_REFUSED apart from a spent launcher budget
        # without re-deriving it from the block reason.
        detail["launcher_failure"] = classification.launcher_failure.value
    if verdict is not None:
        detail["clause"] = verdict.failed_clause
        if verdict.reason:
            detail["verdict"] = verdict.reason
        if verdict.offending_paths:
            # The paths §7.5 says justify calling a permission failure
            # SEMANTIC. They were named in the retry prompt and nowhere
            # durable, so a node that exhausted its semantic budget blocked
            # without ever recording which paths it wrote.
            detail["offending_paths"] = list(verdict.offending_paths)
    return detail


def _wait_any(items: Sequence[Tuple[str, "Future"]],
              timeout: float = 0.05) -> Tuple[List[str], List[str]]:
    """Return the node ids whose futures are done, waiting briefly if none are.

    A short poll rather than `concurrent.futures.wait` on the whole set,
    because the loop must also re-check the merge frontier and the
    cancellation flag while work is in flight.
    """
    done = [node_id for node_id, future in items if future.done()]
    if done:
        return done, [n for n, _ in items if n not in done]
    for _, future in items:
        try:
            future.result(timeout=timeout)
        except Exception:
            pass
        break
    done = [node_id for node_id, future in items if future.done()]
    return done, [n for n, _ in items if n not in done]
