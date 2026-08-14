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

Three seams are injected rather than implemented, because each belongs to
step 7's launcher and would be a lie if faked here (§12.3 — a deferral is
loud, never a stub): running a node, running a gate, and running the
integration gate. They are injected behind the protocols the real adapters
will implement, so the offline suite exercises this module's own logic
rather than a mock of somebody else's.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence,
                    Tuple)

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
    """

    envelope_parsed: bool = True
    exit_code: int = 0
    launched_pid: Optional[int] = None
    launcher_failure: Optional[rp.LauncherFailure] = None


#: `(attempt, node, attempt_record, retry_prompt) -> NodeExecution`. The
#: retry prompt is `None` on a first attempt and non-None only for SEMANTIC
#: retries, which are the only class that mutates it (§7.5).
NodeRunner = Callable[..., NodeExecution]

#: `(attempt, node, phase) -> wt.GateResult`, where phase is "pre" or "post".
GateRunner = Callable[..., "wt.GateResult"]

#: `(integration_path, specs) -> wt.GateResult` (§8.8).
IntegrationGateRunner = Callable[..., "wt.GateResult"]


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
    min_cases: int = 1
    #: `(attempt) -> None`. The watchdog performs the kill the worker cannot,
    #: because the worker thread is blocked reading the agent (§7.6). Killing
    #: an agent's process belongs to the launcher (§9.3, step 7), so it is
    #: supplied rather than implemented here. Left `None`, the watchdog still
    #: detects and fails a stalled attempt; it simply cannot terminate it, and
    #: that is a stated limit rather than a silent one.
    kill_attempt: Optional[Callable[..., None]] = None


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
        self._output_shas: Dict[str, str] = {}
        self._retry_prompts: Dict[str, Optional[str]] = {}
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
        self._projected = True

    def cancel(self) -> None:
        """§7.8 — the scheduler never blocks on a kill. Setting the flag is
        the whole of cancellation's synchronous part; a surviving pane is a
        leak, not a correctness hazard, because a cancelled node's worktree
        is never merged and its result is rejected."""
        self._cancelled.set()

    def shutdown(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)

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
        watchdog, backstop = self._start_liveness()
        try:
            while True:
                if self._cancelled.is_set():
                    for future in list(in_flight.values()):
                        future.cancel()
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

                ready = [node_id for node_id in self.deps.store.ready_nodes(self.run_id)
                         if node_id not in in_flight]
                for node_id in ready:
                    if len(in_flight) >= self.config.concurrency:
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

        def fail(attempt, retry_class, reason):
            # The watchdog returns the node to pending as ENVIRONMENTAL — an
            # attempt that stalled is a fact about the machine, never a
            # verdict about the work (§7.6).
            node = self.nodes.get(attempt.node_id)
            if node is not None:
                self._settle_failure(node, rp.Classification(retry_class=retry_class))

        watchdog = wd.Watchdog(
            config=self.config,
            attempts_provider=running_attempts,
            write_heartbeat=store.record_heartbeat,
            kill=self.deps.kill_attempt or (lambda attempt: None),
            fail_attempt=fail)
        watchdog.start()

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
        """One node's attempt, with the top-level handler §7.5 requires.

        Every worker body carries this handler and **any exception reaching
        it without a classification defaults to ENVIRONMENTAL** — fail-closed,
        never SEMANTIC, so an engine bug can never be recorded as a verdict
        about the code under test. A `ThreadPoolExecutor` otherwise swallows
        an unhandled exception into a future where nobody looks.

        A worker failure **writes only its own node's state**: one node's
        collapse retries or blocks that node while its siblings and the run
        continue (§13.3's negative test).
        """
        node = self.nodes[node_id]
        attempt = None
        try:
            self._attempt_body(node)
        except BaseException as exc:  # noqa: BLE001 — containment is the point
            try:
                signal = rp.FailureSignal(node_kind=node.kind,
                                          exception_type=type(exc).__name__)
                self._settle_failure(node, rp.classify(signal))
            except Exception:
                # Even the failure path cannot be allowed to escape into the
                # future. There is nothing left to record it with, so the run
                # loop sees the node still RUNNING and the watchdog owns it.
                pass
        finally:
            del attempt

    def _attempt_body(self, node: st.PlanNode) -> None:
        store = self.deps.store
        head = wt.integration_head(self.deps.repo, self.deps.integration_branch)

        # §7.6 — the window opens BEFORE the worktree exists, so a hung
        # `git worktree add` is inside it rather than outside.
        attempt_no = store.start_attempt(self.run_id, node.node_id, head)
        record = store.get_attempt(self.run_id, node.node_id, attempt_no)

        attempt = wt.create_attempt_worktree(
            self.deps.repo, self.run_id, node.node_id, attempt_no, head,
            Path(self.deps.worktrees_root), Path(self.deps.scratch_root))

        created = wt.check_at_create(attempt)
        if not created.ok:
            self._settle_failure(node, rp.Classification(
                retry_class=st.RetryClass.ENVIRONMENTAL))
            return

        pre_verdict = None
        if node.kind is st.NodeKind.AGENT:
            pre = self.deps.run_gate(attempt, node, "pre")
            pre_verdict = vf.adjudicate_gate(pre, self.deps.min_cases)
            if pre_verdict.green:
                # §7.4 — terminal and non-retryable, and the agent never runs:
                # a gate that cannot fail proves nothing about work that has
                # not happened yet, so spending an agent on it is pure waste.
                store.mark_blocked(self.run_id, node.node_id,
                                   st.BlockReason.GATE_NOT_FALSIFIABLE)
                return

        baseline = wt.take_baseline(attempt)

        def on_launch(pid: Optional[int] = None) -> None:
            """The adapter reports launch, and that is what arms §7.6's first
            two signals. It has to be a callback rather than a return value:
            the runner does not return until the node is finished, and a
            watchdog that learned of the launch then would have nothing left
            to watch.
            """
            store.mark_launched(self.run_id, node.node_id, attempt_no, pid)

        execution = self.deps.run_node(attempt, node, record,
                                       self._retry_prompts.get(node.node_id),
                                       on_launch)
        if execution.launched_pid is not None:
            # A runner that reports its pid only on return still arms the
            # signals, just late — recorded so the row is complete either way.
            store.mark_launched(self.run_id, node.node_id, attempt_no,
                                execution.launched_pid)

        after = wt.inventory(attempt.path)
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
            self._settle_verdict(node, verdict, execution)
            return

        output_sha = wt.commit_measured_delta(
            attempt, measured, after,
            f"{node.node_id} attempt {attempt_no}")
        wt.check_post_commit(attempt, wt.expected_inventory(baseline, measured, after))

        if node.kind is st.NodeKind.AGENT:
            post = self.deps.run_gate(attempt, node, "post")
            verdict = vf.verify_agent_node(
                envelope_parsed=execution.envelope_parsed,
                pre_gate=pre_verdict,
                post_gate=vf.adjudicate_gate(post, self.deps.min_cases),
                permission=permission)
            if not verdict.verified:
                self._settle_verdict(node, verdict, execution)
                return

        with self._lock:
            self._output_shas[node.node_id] = output_sha
        store.mark_verified(self.run_id, node.node_id, output_sha)

    # ── settling a failed attempt ───────────────────────────────────────────

    def _settle_verdict(self, node: st.PlanNode, verdict: "vf.VerificationVerdict",
                        execution: NodeExecution) -> None:
        """Turn a failed VERIFIED predicate into a classification (§7.5)."""
        if verdict.block_reason is not None:
            self.deps.store.mark_blocked(self.run_id, node.node_id,
                                         verdict.block_reason)
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
        self._settle_failure(node, classification, verdict)

    def _settle_failure(self, node: st.PlanNode, classification: rp.Classification,
                        verdict: Optional["vf.VerificationVerdict"] = None) -> None:
        """Block or release the node — decided here, while it is still RUNNING.

        The store legally transitions to BLOCKED only from RUNNING, and that
        is correct: a node blocks out of an attempt, not out of the queue. So
        the budget question has to be answered now rather than at the next
        pick-up, when the node would be PENDING and the transition refused.
        """
        store = self.deps.store
        if classification.block_reason is not None:
            store.mark_blocked(self.run_id, node.node_id, classification.block_reason)
            return

        retry_class = classification.retry_class or st.DEFAULT_RETRY_CLASS
        if retry_class is st.RetryClass.SEMANTIC:
            lifecycle = store.get_node(self.run_id, node.node_id)
            if self._semantic_ceiling_reached(node.node_id,
                                              lifecycle.granted_extra_attempts):
                store.mark_blocked(self.run_id, node.node_id,
                                   st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
                                   retry_class=retry_class)
                return
            # Only SEMANTIC mutates the prompt, and the offending paths are
            # named in it — which is what makes the retry genuinely new
            # instructions rather than the same request repeated (§7.5).
            self._retry_prompts[node.node_id] = _retry_prompt(node, verdict)
        else:
            budget = self.config.retry_budget(retry_class)
            spent = store.attempts_spent(self.run_id, node.node_id, retry_class)
            if spent >= budget:
                store.mark_blocked(self.run_id, node.node_id,
                                   _budget_reason(retry_class),
                                   retry_class=retry_class)
                return

        store.fail_attempt(self.run_id, node.node_id, retry_class)

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
        while True:
            records = self.deps.store.node_records(self.run_id)
            candidate = wt.merge_ready(records)
            if candidate is None:
                return
            with self._lock:
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
            self.deps.store.mark_merged(self.run_id, candidate.node_id)

    # ── §8.8 final acceptance, and §7.3's declaration ───────────────────────

    def _declare(self) -> RunReport:
        store = self.deps.store
        records = store.node_records(self.run_id)
        states = {r.node_id: r.state for r in records}
        merged = tuple(sorted(n for n, s in states.items()
                              if s == st.NodeState.MERGED.value))
        cancelled = tuple(sorted(n for n, s in states.items()
                                 if s == st.NodeState.CANCELLED.value))
        blocked = tuple(sorted(
            (n, store.get_node(self.run_id, n).block_reason)
            for n, s in states.items() if s == st.NodeState.BLOCKED.value))

        acceptance = None
        if self._is_candidate_accepted(states):
            acceptance = self._run_final_acceptance(records, merged)

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
            ancestry=dict(acceptance.ancestry) if acceptance else {})

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
        store.acceptance_started(self.run_id)
        deadline = time.monotonic() + self.config.final_acceptance_timeout_s

        with self._lock:
            shas = {n: self._output_shas[n] for n in merged if n in self._output_shas}
        ancestry = wt.final_ancestry_sweep(Path(self.deps.integration_path), shas)
        if not all(ancestry.values()) or len(ancestry) != len(merged):
            return AcceptanceResult(
                green=False, specs=(), ancestry=ancestry,
                reason="the final ancestry sweep did not re-prove every merged node")
        if time.monotonic() > deadline:
            return AcceptanceResult(green=False, specs=(), ancestry=ancestry,
                                    reason="final-acceptance timeout during the sweep")

        specs = wt.acceptance_specs(records)
        gate = self.deps.run_integration_gate(Path(self.deps.integration_path), specs)
        verdict = vf.adjudicate_gate(gate, self.deps.min_cases)
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


def _budget_reason(retry_class: st.RetryClass) -> st.BlockReason:
    if retry_class is st.RetryClass.LAUNCHER_TRANSIENT:
        return st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED
    return st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED


def _retry_prompt(node: st.PlanNode,
                  verdict: Optional["vf.VerificationVerdict"]) -> str:
    """The mutated prompt for a SEMANTIC retry, naming what went wrong.

    Naming the offending paths is the whole justification for classifying an
    agent's permission failure as SEMANTIC rather than blocking it: a retry
    that repeats the same request is not new instructions, and spending an
    attempt on it would be the refund loop §7.5 convicts.
    """
    lines = [f"Attempt for node {node.node_id} did not verify."]
    if verdict is not None:
        if verdict.reason:
            lines.append(verdict.reason)
        if verdict.offending_paths:
            lines.append("Paths written outside this node's declared outputs: "
                         + ", ".join(verdict.offending_paths))
    lines.append("Declared outputs: " + (", ".join(node.outputs) or "(none)"))
    return "\n".join(lines)


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
