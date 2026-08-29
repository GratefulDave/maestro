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
    record_baseline                 # §8.3 — and is written down, because
                                    #   the provisioned tree is in no commit
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

import posixpath
import signal
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import code_review as cr
from . import gate_capture
from . import launcher as lch
from . import lifecycle as lc
from . import plan_model as pm
from . import reachability as rc
from . import retry_policy as rp
from . import scheduler_types as st
from . import verification as vf
from . import tests_chain as tc
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
    #: The agent's terminal envelope, parsed — §7.7's result payload.
    #:
    #: The ruling item 43 held open, taken the same way item 42's was: by what
    #: may read it. This is DATA, recorded in the `results` row beside its
    #: adjudication and read back by nothing that decides anything. It is not
    #: `launch_detail`'s prose and it is not a classifier input; §7.7 requires
    #: the payload to be retained in all four adjudications, so it has to
    #: travel, and the only component that possesses it is the adapter that
    #: read the envelope file.
    #:
    #: `_poll_agent_execution` already parsed exactly this object and threw it
    #: away, keeping only `envelope_parsed`. That discard is why the `results`
    #: table had a live reader in `run status` and no writer at all: not a
    #: missing mechanism, a dropped value — §7.6's lesson about a typed reason
    #: computed and never recorded, in its second instance.
    envelope_payload: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class RepairExecution:
    """A persistent builder's typed response to one repair handoff.

    The acknowledgement is deliberately returned beside, but not folded into,
    ``NodeExecution``: an agent envelope describes one completed node
    execution, while a repair acknowledgement authorizes delivery of findings
    about a *previous immutable candidate*.  Joining the two would let a
    stale acknowledgement be mistaken for the current execution's result.
    """

    execution: NodeExecution
    acknowledged_rejected_sha: str
    builder_generation: int


#: Runner boundaries receive the scheduler's cancellation lease explicitly.
#: They MUST use it while waiting so no gate or owned execution can outlive
#: the RUNNING generation that authorized it.
CancelRequested = Callable[[], bool]
NodeRunner = Callable[
    [
        wt.AttemptWorktree,
        st.PlanNode,
        st.AttemptRecord,
        Optional[str],
        Callable[[Optional[int]], None],
        CancelRequested,
    ],
    NodeExecution,
]
GateRunner = Callable[
    [
        wt.AttemptWorktree,
        st.PlanNode,
        str,
        CancelRequested,
    ],
    "wt.GateResult",
]
IntegrationGateRunner = Callable[
    [
        Path,
        Sequence[str],
        CancelRequested,
    ],
    "wt.GateResult",
]
QuiesceAttempt = Callable[[st.AttemptRecord, str], None]
#: ``(attempt, node, record, base_sha, output_sha,
#: resume_existing_dispatch) -> code_review.ReviewOutcome``.
#: The final flag distinguishes a newly-created dispatch from an unfinished
#: durable dispatch whose retained actor/report must be adopted on resume.
#: Raises `code_review.ReviewStalled` when the reviewer never reported, which
#: the scheduler classifies ENVIRONMENTAL — a wedged reviewer is a fact about
#: the machine, and must not spend a node's review budget.
ReviewRunner = Callable[
    [
        wt.AttemptWorktree,
        st.PlanNode,
        st.AttemptRecord,
        str,
        str,
        bool,
    ],
    Any,
]

#: Resume the existing builder pane/session after a candidate rejection.
#: The repair acknowledgement names the rejected immutable candidate and the
#: generation which accepted it, so a late response can never authorize a
#: different handoff.
ContinueRunner = Callable[
    [
        wt.AttemptWorktree,
        st.PlanNode,
        st.AttemptRecord,
        str,
        str,
        int,
        CancelRequested,
    ],
    RepairExecution,
]
CloseReview = Callable[[str], None]


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


class UnserviceableHandoff(AttemptOwnershipLost):
    """A repair handoff reached a delivery path that cannot service it.

    A subclass of `AttemptOwnershipLost` on purpose, so it keeps crossing
    every deliberate re-raise between the repair callback and the worker's
    top-level handler; only that handler, and `_continue_repair_handoff`,
    distinguish it. The distinction is the whole point. Ordinary ownership
    loss means *another* generation now owns this attempt and there is
    nothing to record; this means nothing owns it and nothing ever will,
    which is a terminal fact about the lane and must appear in the ledger as
    a typed transition rather than be inferred from a dead process.
    """


class RunPaused(RuntimeError):
    """SIGINT or `request_pause` stopped the run without making it terminal.

    Raised instead of returning a `RunReport` because there is no outcome to
    report: a pause writes none, and a `RunReport` carrying one would be the
    lie. Nothing about the run's lifecycle moves — `latest_outcome` stays
    NULL, no node row is rewritten, `cancel_requested` is never set — so a
    paused run is indistinguishable in the ledger from one whose scheduler
    process simply stopped, and `run resume` is legal against it for exactly
    that reason (§1.2, §7.8).
    """


class SigintInterrupt(BaseException):
    """Injected by the SIGINT handler so a blocking wait unblocks.

    Not `KeyboardInterrupt`: pytest treats that as a session abort, so a
    test that delivers a real SIGINT would never get to assert the pause.
    `BaseException` so `except Exception` cannot swallow it. The handler
    is the only raiser; the run loop is the only catcher.
    """


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

    @property
    def pane_created(self) -> bool:
        """Whether a pane existed when the launch failed (§16.3 item 45).

        Read from the launcher's own typed refusal, which arrives here as this
        exception's `__cause__` because the wrapper chains it (`raise ... from
        exc`). One representation of the fact, at the only layer that knows
        it: the launcher raised the refusal and is the only thing that can say
        whether it had split a pane first.

        `True` for everything else, and the default is the whole point. §8.3's
        quiesce step exists to prove an attempt's owned execution absent, and
        a launch failure that has not *stated* it created nothing is a launch
        failure that may have left a pane. Reporting absence nobody measured
        is the shape §8.3 refuses on principle, so the fall-through keeps
        today's behaviour: prove it.
        """
        cause = self.__cause__
        if isinstance(cause, lch.LaunchRefused):
            return cause.pane_created
        return True

    @property
    def classified_failure(self) -> rp.LauncherFailure:
        """The member the retry budget is sized from (§16.3 item 46).

        `failure` is what the adapter's own `classify` made of the exception,
        and it is wrong in one direction only: `ErrorClass.CONFIGURATION` maps
        to STARTUP, which carries a retry budget, while a refusal that is
        deterministic by construction cannot succeed on any attempt. The
        launcher states that determinism as a typed field on its refusal, so
        the member is upgraded from a structural fact rather than inferred
        from the refusal's message — branching on
        `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:` would be the lexical
        shortcut §7.5 forbids.

        This is a member of LAUNCHER_TRANSIENT, not a fourth retry class:
        §7.5's closure at three holds, and the budget varies by member exactly
        as CREDENTIAL's zero already does.
        """
        cause = self.__cause__
        if isinstance(cause, lch.LaunchRefused) and cause.deterministic:
            return rp.LauncherFailure.DETERMINISTIC_REFUSAL
        return self.failure


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
    #: Re-read a declaration from an existing attempt; never launches.
    recover_node: Optional[
        Callable[[wt.AttemptWorktree], Optional[NodeExecution]]
    ] = None

    #: The canonical bytes of the plan this run executes, retained on the run.
    #:
    #: The plan was the one artifact a run depended on that was referenced by
    #: content and not *stored* by content — git holds every sha, the receipt
    #: store holds every review digest, and `runs.plan_digest` resolved through
    #: a file path that `plan ship` overwrites. Amending a plan therefore
    #: destroyed the run's only handle on it and `_resolve_resume_target`
    #: refused forever.
    #:
    #: `None` leaves the old behaviour untouched, which is what keeps every
    #: caller that does not supply bytes — the offline suite included — working
    #: exactly as before.
    plan_bytes: Optional[bytes] = None

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
    #: `(node_id, attempt_no, phase, detail) -> None`. Display only.
    #:
    #: Everything between claiming an attempt and opening its pane is silent
    #: work that takes minutes: provisioning a tree, running the pre-gate,
    #: walking the baseline. An operator watching a terminal cannot tell that
    #: window from a hang, and on
    #: `run-9d03105407f440079f3730f1fe4c67b3` two attempts were killed by hand
    #: inside it before a fault in the pre-gate was found that really did hang
    #: there -- the same silence covering both.
    #:
    #: This is a reporter and never an input: §1.2 forbids a lifecycle
    #: transition keyed on anything but a typed record, and nothing the
    #: scheduler decides reads it back. `_say` swallows whatever it raises for
    #: the same reason -- a terminal that cannot print must not fail an
    #: attempt.
    progress: Optional[Callable[[str, int, str, Mapping[str, object]], None]] = None
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
    #: Submit a repair handoff to the existing builder session.  A replacement
    #: builder is an actor-session recovery concern, never an attempt retry.
    continue_node: Optional[ContinueRunner] = None
    #: Close the lane's persistent reviewer only after the lane is terminal.
    close_review: Optional[CloseReview] = None
    #: Resolves immutable review evidence to its confined signed receipt path.
    #: The review outcome intentionally carries a receipt value, not storage
    #: ownership, so this remains a runtime-provided seam.
    receipt_path_for: Optional[Callable[[str], str]] = None
    #: Raw per-pane agent status for the watchdog's turn clock (#107 / B14).
    #: Never `observe()` — that call collapses idle into RUNNING. `None` is
    #: not an observation of work: the clock still fires. `maestro run start`
    #: supplies the reader; omitting it is the M30 failure mode.
    actor_status: Optional[Callable[[st.AttemptRecord], Optional[str]]] = None
    #: The plan's `base_commit`. The revert target for a `controlled_mutation`
    #: negative control, and nothing else reads it.
    #:
    #: It is the plan's base rather than the attempt's, and the difference is
    #: the whole point of the strategy: reverting to the attempt's own base
    #: restores a tree in which the behaviour under test may already exist, so
    #: the "control" would change nothing and every candidate would fail it
    #: for the same uninformative reason. Empty is refused at the point of
    #: use, by name, rather than silently reverting to `HEAD`.
    plan_base_commit: str = ""

    def __post_init__(self) -> None:
        if self.quiesce_attempt is None:
            raise QuiescenceDependencyError(
                "quiesce_attempt is required: a scheduler cannot classify or "
                "retry while owned execution may still exist"
            )


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


class TestStrengthContractMismatch(RuntimeError):
    """A run's durable pin and its plan disagree about the test contract.

    Refused rather than reconciled, and refused in the direction that leaves
    the run exactly as it was. Both possible reconciliations are wrong:
    adopting the plan's contract decides an already-terminal node's dependants
    under rules that node was never judged by, and adopting the pin runs a
    contracted plan under the rules it was written to replace.
    """


class MixedTestStrengthContract(ValueError):
    """Some tests nodes declare a strength contract and some do not.

    Unrepresentable in a plan — `maestro-plan.v4` requires the field of every
    tests node and v3 has no field to declare — so this can only be a
    hand-built node set, and it has no defensible answer. Enforcing the
    contract for half a run would accept the other half on its case count
    while reporting the run as contracted, which is worse than either
    consistent choice.
    """


def derive_test_strength_contract(
    nodes: Sequence[st.PlanNode],
) -> st.TestStrengthContract:
    """Which contract this plan's tests nodes are judged under.

    A plan with no tests nodes is `STRENGTH_V1`: there is nothing weaker
    about it, and answering LEGACY would report every build-only run as
    running under superseded rules.
    """
    tests = [node for node in nodes if node.kind is st.NodeKind.TESTS]
    if not tests:
        return st.TestStrengthContract.STRENGTH_V1
    declared = [node for node in tests if node.test_strength is not None]
    if not declared:
        return st.TestStrengthContract.LEGACY
    if len(declared) != len(tests):
        raise MixedTestStrengthContract(
            "these tests nodes declare a test-strength contract and these do "
            "not: {0} vs {1}".format(
                ", ".join(sorted(n.node_id for n in declared)),
                ", ".join(sorted(n.node_id for n in tests
                                 if n.test_strength is None))))
    return st.TestStrengthContract.STRENGTH_V1


@dataclass(frozen=True)
class _DerivedReviewNode:
    """Scheduler-owned review edge; never an authored ``PlanNode``.

    ``PlanNode`` deliberately refuses ``NodeKind.REVIEW`` so an authored plan
    cannot make review optional.  The lifecycle projection nevertheless needs
    the same graph fields as a plan node.  Keeping this private structural
    value here preserves that authored-schema boundary while making the
    scheduler's derived edge explicit and durable.
    """

    node_id: str
    review_of: str
    depth: int
    needs: Tuple[str, ...]
    specs: Tuple[str, ...] = ()


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
    #: §8.3's pre-merge cleanliness evaluation, `node_id -> diverging paths`.
    #: Residue found after the post-node gate is an adapter hygiene defect,
    #: not a verdict about the node: the commit was sealed before the gate ran
    #: and the merge consumes committed objects, so the node merges on its
    #: measured content and this is what the operator is told instead. Empty in
    #: the ordinary case, and empty is the claim that the adapters are clean.
    adapter_hygiene: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    #: `node_id -> findings` for every node a reviewer rejected, merged or not.
    #: Reported for the same reason the acceptance result is: a node's state
    #: names what happened to it, never the thing to fix, and a lane whose
    #: reviewer objected deserves to say what about without the operator
    #: re-running it. It used to be populated only where `review_ceiling` had
    #: blocked the node, which is the one case where the findings were least
    #: useful — the node was already dead. A reviewer that can no longer block
    #: anything (§19 M35) reports on everything it read instead.
    review_findings: Dict[str, str] = field(default_factory=dict)
    #: `node_id -> findings-per-attempt`, one entry per review-rejected
    #: attempt in order. `review_findings` says what the last reviewer
    #: objected to; this says whether the objections were shrinking. A
    #: descending series is a node the review ceiling cut off early, a flat
    #: one is a node more attempts would not have saved, and that is the
    #: difference an operator needs to size `review_ceiling` from a run
    #: rather than by hand. Rebuilt from the attempt rows on resume, so it
    #: reports the whole run rather than whichever process finished it.
    review_convergence: Dict[str, Tuple[int, ...]] = field(default_factory=dict)
    #: Scheduler-derived review-node state by stable review id.  These nodes
    #: are not sources and therefore never appear in ``merged``.
    review_nodes: Dict[str, str] = field(default_factory=dict)


class Scheduler:
    """One run. Construct, `run()`, read the report.

    Not reusable across runs on purpose: a scheduler holds the run's thread
    pool, its cancellation flag, and its per-node output SHAs, and reusing
    one would be the overloaded role §4's RC4 convicts.
    """

    @staticmethod
    def _review_id(build_node_id: str) -> str:
        return f"{build_node_id}::review"

    @staticmethod
    def _is_derived_review(node: Any) -> bool:
        return isinstance(node, _DerivedReviewNode)

    def _derived_review_nodes(self) -> Tuple[_DerivedReviewNode, ...]:
        return tuple(
            node for node in self.nodes.values() if self._is_derived_review(node)
        )

    def _downstream_authored_nodes(self, build_node_id: str) -> Tuple[str, ...]:
        return tuple(
            node.node_id
            for node in self.authored_nodes.values()
            if build_node_id in node.needs
        )

    def _runnable_ready_nodes(self) -> Tuple[str, ...]:
        """Ready authored nodes, treating ACCEPTED review edges as satisfied."""
        # Preserve the lifecycle store's ready-query boundary: operators and
        # cancellation tests may observe between-frontier readiness there.
        self.deps.store.ready_nodes(self.run_id)
        return tuple(
            record.node_id
            for record in self.deps.store.node_records(self.run_id)
            if (
                record.state == st.NodeState.PENDING.value
                and record.node_id in self.nodes
                and not self._is_derived_review(self.nodes[record.node_id])
                and all(
                    self._dependency_satisfied(dependency)
                    for dependency in record.needs
                )
            )
        )

    def _review_for_build(self, build_node_id: str) -> Optional[_DerivedReviewNode]:
        review = self.nodes.get(self._review_id(build_node_id))
        return review if self._is_derived_review(review) else None

    def _review_is_accepted(self, review_node_id: str) -> bool:
        return (
            self.deps.store.get_node(self.run_id, review_node_id).state
            is st.NodeState.ACCEPTED
        )

    def _merge_records(
        self, records: Sequence["wt.NodeRecord"]
    ) -> Tuple["wt.NodeRecord", ...]:
        """Present accepted review edges as satisfied, never merge candidates."""
        return tuple(
            record.with_state(st.NodeState.MERGED.value)
            if (
                self._is_derived_review(self.nodes.get(record.node_id))
                and record.state == st.NodeState.ACCEPTED.value
            )
            else record
            for record in records
        )

    def _out_of_contract_review_owner(self, node_id: str) -> Optional[str]:
        """The tests node owning a review row this run's contract excludes.

        A ledger is not repaired by a code change. A legacy-pinned run that
        was resumed by the runtime this fixes carries the damage durably: a
        `<tests>::review` row it will never dispatch, and a dependant whose
        `needs_json` was rewired to point at it. Not deriving the node any
        more leaves that row orphaned, and an orphan is worse than the bug --
        `_dependency_satisfied` would answer "not merged" about a review that
        never merges, and `_is_candidate_accepted` would refuse the whole run
        for a row in neither terminal state.

        So the exclusion has to be stated once and honoured everywhere: in a
        run whose contract has no test reviewer, a review row over a tests
        node is not part of the run, and the dependency it was spliced into is
        satisfied by the tests node itself -- exactly the edge the plan
        authored (§19 M42).

        Deliberately not a database repair. Rewriting `needs_json` under a
        legacy run would be the migration the rollout invariant reserves for
        an operator who asked for one; declining to *require* a node is not.
        """
        if self._tests_nodes_are_reviewable():
            return None
        if node_id in self.nodes or not node_id.endswith("::review"):
            return None
        owner = node_id[: -len("::review")]
        return owner if self._is_tests_node(owner) else None

    def _dependency_satisfied(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        if self._is_derived_review(node):
            if not (
                self._review_is_accepted(node_id)
                and _is_merged(self.deps.store, self.run_id, node.review_of)
            ):
                return False
            return self._test_prerequisite_satisfied(node.review_of)
        owner = self._out_of_contract_review_owner(node_id)
        if owner is not None:
            return _is_merged(self.deps.store, self.run_id, owner)
        if self._is_tests_node(node_id):
            return _is_merged(
                self.deps.store, self.run_id, node_id
            ) and self._test_prerequisite_satisfied(node_id)
        return _is_merged(self.deps.store, self.run_id, node_id)

    def _is_tests_node(self, node_id: str) -> bool:
        node = self.authored_nodes.get(node_id)
        return node is not None and node.kind is st.NodeKind.TESTS

    def _test_prerequisite_satisfied(self, node_id: str) -> bool:
        """Whether a tests node is a *completed* prerequisite (obligation 10).

        Typed lifecycle state, not a generic MERGED inference. MERGED says the
        candidate's commit reached the integration branch; it says nothing
        about whether the candidate discriminates, and a consuming
        implementation admitted on that alone is admitted on the strength of
        tests nobody proved could fail. So the question asked here is the one
        that matters: does this node have an accepted test candidate — strong
        measured evidence *and* a passed independent review, both bound to the
        same immutable sha.

        A legacy-pinned run answers on MERGED alone, deliberately. Its tests
        nodes were admitted under the contract they were created under, and
        reaching back through an already-admitted dependency is what the
        rollout invariant forbids.
        """
        if not self._is_tests_node(node_id):
            return True
        if self.test_strength_contract is not st.TestStrengthContract.STRENGTH_V1:
            return True
        if self.deps.store.accepted_test_candidate(self.run_id, node_id) is None:
            return False
        return not any(
            block.tests_node_id == node_id
            for block in self.deps.store.legacy_test_strength_blocks(self.run_id)
        )

    def _governing_test_strength_contract(self) -> st.TestStrengthContract:
        """The contract this run's nodes are judged under, pin first.

        An existing run answers with its pin, unconditionally and without
        consulting the plan. A run that does not exist yet has no pin to
        honour and answers with what its plan implies -- which is the value
        `project()` is about to write as the pin.

        The two disagree only when a plan was re-shipped, or a newer runtime
        derives a different contract from the same authored nodes, under a run
        that already exists. `project()` refuses that resume outright; this
        method makes the refusal reachable, because until it happens the
        scheduler must behave as the run was created, not as the plan now
        reads.
        """
        pinned = self.deps.store.pinned_test_strength_contract(self.run_id)
        if pinned is None:
            return self.plan_test_strength_contract
        return pinned

    def _tests_nodes_are_reviewable(self) -> bool:
        """Whether *this run* derives review edges over its tests nodes.

        Only a `STRENGTH_V1` run does. The rollout invariant is that a
        legacy-pinned run keeps the node set, depths and dependency edges it
        was authored and admitted with: its tests nodes were accepted on their
        case count, its dependants were admitted on that acceptance, and there
        is no verdict a review opened now could reach that would not be a
        retroactive reclassification of an already-terminal row.

        Deriving one anyway is not a harmless extra node. It inserts a
        `PENDING` review into the run's frontier, rewires every direct
        dependant of the tests node to need the review instead, and lengthens
        every path below it. The pre-resume audit of
        `run-8d1a71f463e4430f92a125a8f8b3731d` found that the old projection
        would do exactly that to its 13 legacy tests nodes; the live ledger
        remained unmodified because resume was withheld (§19 M42).
        """
        return self.test_strength_contract is st.TestStrengthContract.STRENGTH_V1

    def _project_nodes(
        self, authored_nodes: Mapping[str, st.PlanNode]
    ) -> Dict[str, Any]:
        """Overlay mandatory review edges without mutating authored nodes."""
        if self.deps.review_attempt is None:
            return dict(authored_nodes)

        # Tests nodes are reviewable, and their absence from this set was the
        # defect. A derived review node is the only thing in this design that
        # makes "no lane merges unreviewed" a property of every run rather
        # than of an author remembering to declare one -- and until now it was
        # derived for build lanes only, so a tests node went VERIFIED ->
        # MERGED with no independent reader at all.
        # Run-8d1a71f463e4430f92a125a8f8b3731d is what that costs:
        # `lane-acquisition-manifest-tests` reached MERGED on four non-skipped
        # cases, and the four implementation candidates it existed to gate
        # were each independently rejected. The tests were never judged.
        #
        # Scoped to the run's *pinned* contract, because a legacy run's tests
        # nodes were admitted under rules that had no reviewer in them at all.
        reviewable_kinds = (
            (st.NodeKind.AGENT, st.NodeKind.TESTS)
            if self._tests_nodes_are_reviewable()
            else (st.NodeKind.AGENT,)
        )
        reviewable = {
            node_id
            for node_id, node in authored_nodes.items()
            if node.kind in reviewable_kinds
        }
        review_ids = {node_id: self._review_id(node_id) for node_id in reviewable}
        collisions = set(authored_nodes).intersection(review_ids.values())
        if collisions:
            raise ValueError(
                "an authored node collides with the scheduler-derived review id: "
                + ", ".join(sorted(collisions))
            )

        needs = {
            node_id: tuple(
                review_ids.get(dependency, dependency) for dependency in node.needs
            )
            for node_id, node in authored_nodes.items()
        }
        projected: Dict[str, Any] = dict(authored_nodes)
        for build_node_id in sorted(reviewable):
            build = authored_nodes[build_node_id]
            projected[review_ids[build_node_id]] = _DerivedReviewNode(
                node_id=review_ids[build_node_id],
                review_of=build_node_id,
                depth=build.depth + 1,
                needs=(build_node_id,),
                specs=(),
            )

        # A derived edge lengthens every path below the reviewed build.  Keep
        # authored depths as lower bounds, then lift only runtime projection
        # depths needed to retain a truthful topological order.
        source_depth = {node_id: node.depth for node_id, node in projected.items()}
        projected_needs = {
            **needs,
            **{
                review_id: (build_node_id,)
                for build_node_id, review_id in review_ids.items()
            },
        }
        visiting = set()
        resolved_depth: Dict[str, int] = {}

        def depth_for(node_id: str) -> int:
            if node_id in resolved_depth:
                return resolved_depth[node_id]
            if node_id in visiting:
                raise ValueError(f"{node_id}: dependency graph contains a cycle")
            visiting.add(node_id)
            dependencies = projected_needs[node_id]
            depth = source_depth[node_id]
            if dependencies:
                try:
                    depth = max(depth, max(depth_for(dep) + 1 for dep in dependencies))
                except KeyError as exc:
                    raise ValueError(
                        f"{node_id}: dependency {exc.args[0]!r} is not a plan node"
                    ) from exc
            visiting.remove(node_id)
            resolved_depth[node_id] = depth
            return depth

        for node_id in projected:
            depth_for(node_id)
        for node_id, node in tuple(projected.items()):
            node_needs = projected_needs[node_id]
            if self._is_derived_review(node):
                projected[node_id] = replace(
                    node, depth=resolved_depth[node_id], needs=node_needs
                )
            elif node.needs != node_needs or node.depth != resolved_depth[node_id]:
                projected[node_id] = replace(
                    node, depth=resolved_depth[node_id], needs=node_needs
                )
        return projected

    def __init__(
        self,
        run_id: str,
        nodes: Sequence[st.PlanNode],
        config: st.SchedulerConfig,
        deps: SchedulerDeps,
        plan_digest: str = "",
        plan_name: Optional[str] = None,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self.run_id = run_id
        self.config = config
        self.deps = deps
        # PlanNode remains the exact authored schema.  `self.nodes` is the
        # runtime DAG that additionally owns derived review edges.
        self.authored_nodes: Dict[str, st.PlanNode] = {
            node.node_id: node for node in nodes
        }
        #: What the plan in front of this process implies, and the value
        #: pinned into `runs.test_strength_contract` when this process is the
        #: one creating the run.
        self.plan_test_strength_contract = derive_test_strength_contract(nodes)
        #: Which test-acceptance rules this run's nodes are actually judged
        #: under.
        #:
        #: The durable pin whenever the run already exists, and only the
        #: plan's implication when it does not. A plan is mutable -- it can be
        #: re-shipped from its IR, and a newer runtime can derive a different
        #: contract from the same authored nodes -- while the pin is written
        #: once, at `create_run`, and is the fact under which this run's
        #: already-terminal nodes were decided. Reading the plan here is what
        #: let a legacy-pinned run be *projected* under `STRENGTH_V1` rules
        #: before `project()` ever got the chance to refuse the mismatch, and
        #: projection is not a read: it derives review nodes and rewires the
        #: authored dependency edges of everything downstream of them.
        self.test_strength_contract = self._governing_test_strength_contract()
        # Projected *after* the governing contract is known, because the
        # contract decides whether tests nodes own derived review edges at all.
        self.nodes: Dict[str, Any] = self._project_nodes(self.authored_nodes)
        self.plan_digest = plan_digest
        self.plan_name = plan_name
        # Defaults to `time.time` so it matches `started_at` and
        # `last_transition_at` (epoch seconds). Tests inject a fake.
        self._time_source = time_source

        self._cancelled = threading.Event()
        #: Set beside `_cancelled`, never instead of it. The run loop's whole
        #: stopping machinery keys on `_cancelled`, so a pause reuses it to
        #: stop new work and quiesce the workers; this second flag is what
        #: tells the loop not to write `cancel_run` on the way out, and the
        #: exit to raise `RunPaused` rather than declare.
        self._paused = threading.Event()
        #: Signal-handler-safe latches. The handler must not take `_lock` or
        #: call `Event.set`: either deadlocks if the interrupted thread holds
        #: the matching lock. One Ctrl-C under `uv run` delivers SIGINT twice
        #: (process-group + uv forward), so escalation is armed only after the
        #: run loop has observed the pause, never on the second delivery of
        #: the same chord.
        self._sigint_pause_latched = False
        self._sigint_escalate_armed = False
        self._sigint_escalated = False
        self._sigint_handler_installed = False
        self._lock = threading.RLock()
        # A watchdog timeout revokes a generation before quiescence can
        # complete and release its retry. The durable row remains RUNNING
        # during that proof, so its state alone cannot stop a provisioner
        # unblocked by the watchdog from entering a gate or runner.
        self._watchdog_fences: Dict[Tuple[str, str, int], None] = {}
        #: `(node_id, attempt_no) -> whether the runner was ever entered`.
        #:
        #: §8.3's settle proves the attempt's **dispatched** execution absent:
        #: the pane or the process `run_node` opened. An attempt this scheduler
        #: leased and never dispatched has none, and by the time any settle is
        #: reached the pre-dispatch execution contexts have already been
        #: accounted for — `check_at_create` fails before provision or the pre
        #: gate has started, and every later pre-dispatch exit leaves through
        #: the `finally` that takes the `pre-baseline` proof. So a settle there
        #: is a question about nothing, and the runtime's quiescer answers a
        #: question about nothing with `PROCESS_GROUP_UNTRACKED` — which reads
        #: as "absence unproven" and blocks the node terminally, burying the
        #: verdict the attempt had actually reached.
        #:
        #: Consulted by the watchdog's kill and by `fail`'s `watchdog`
        #: quiesce. An attempt this scheduler leased and has not entered
        #: `run_node` opened no pane and no agent process group. Asking the
        #: runtime quiescer anyway raises `PROCESS_GROUP_UNTRACKED` and the
        #: contain path turns a retryable NODE_TIMEOUT into terminal
        #: `QUIESCENCE_UNPROVEN` (lane-wp6-build#1,
        #: run-9d03105407f440079f3730f1fe4c67b3). Provision bounds itself at
        #: 600s; node gates (pre/post/falsify) at `NODE_GATE_TIMEOUT_S` (600s)
        #: via `run_harness_process`, which reaps the group on timeout. They
        #: are not this map. `pre-baseline` and `cancel` still demand their
        #: own proofs.
        #:
        #: A key absent from this map is an attempt **this scheduler did not
        #: lease** — an inherited RUNNING row from another process, whose owned
        #: execution this scheduler cannot account for. It keeps demanding the
        #: proof: the exemption is knowledge, never a default.
        self._attempt_dispatch: Dict[Tuple[str, int], bool] = {}
        self._output_shas: Dict[str, str] = {}
        #: The latest attempt worktree per node, so §8.8's cleanup has
        #: something to remove. Replaced on every attempt: an earlier attempt's
        #: checkout is superseded by the one that produced the merged SHA.
        self._attempt_worktrees: Dict[str, "wt.AttemptWorktree"] = {}
        # Latest typed guidance per acceptance surface, per node. Replaced a
        # plain last-failure string: verification and review took turns
        # overwriting one slot, and each attempt fixed exactly what the last
        # prompt named while regressing the constraint the prompt no longer
        # mentioned (run-32b19abadf4a4d6b801ae0f4456976c7 BLOCKED after five
        # such attempts). Rendered into the retry prompt at dispatch and read
        # nowhere else — nothing transitions on it (§10.1/§1.2).
        #: Keyed by `(node_id, base_sha)` — `AttemptRecord.guidance_key` —
        #: never by node alone. See that property for why the base belongs in
        #: the key.
        self._guidance: Dict[Tuple[str, str], rp.GuidanceLedger] = {}
        #: Findings from the review that exhausted a node's budget, kept so the
        #: run report can surface *why* rather than only that a count ran out.
        self._review_findings: Dict[str, str] = {}
        #: `node_id -> every harness-hygiene fact observed about that node`.
        #: Two surfaces write here — §8.3's pre-merge residue report and
        #: §8.8's cleanup refusal — and they **accumulate** rather than
        #: replace. A single slot two independent surfaces assign to is the
        #: shape that made `_guidance` lose a standing constraint every time
        #: the other surface fired; it is no more acceptable for a report than
        #: it was for a retry prompt, and it was caught here by a leaky-gate
        #: test whose residue entry a cleanup refusal had overwritten.
        self._adapter_hygiene: Dict[str, Tuple[str, ...]] = {}
        self._pool: Optional[ThreadPoolExecutor] = None
        self._projected = False
        self._stuck = False
        _refuse_colliding_integration_branch(deps.integration_branch, run_id)

    # ── lifecycle of the scheduler itself ───────────────────────────────────

    def _reconcile_accepted_pass_crash(self) -> None:
        """Complete the sole crash seam after an accepted PASS.

        Lane phase must be written while the builder still owns its RUNNING
        attempt.  A process can therefore die after that terminal phase is
        durable but before the source node reaches VERIFIED.  The sealed
        attempt output and exact accepted review are sufficient authority to
        complete only that interrupted transition; anything less is blocked
        rather than left as an invisible RUNNING node on resume.
        """
        store = self.deps.store
        for node in self.authored_nodes.values():
            review_node = self._review_for_build(node.node_id)
            if review_node is None:
                continue
            lifecycle = store.get_node(self.run_id, node.node_id)
            if (
                lifecycle.state is not st.NodeState.RUNNING
                or lifecycle.lane_phase is not st.LanePhase.ACCEPTED
                or not lifecycle.attempt_no
            ):
                continue
            try:
                attempt = store.get_attempt(
                    self.run_id, node.node_id, lifecycle.attempt_no
                )
            except lc.UnknownNode:
                attempt = None
            output_sha = (
                None
                if attempt is None
                else store.attempt_sealed_output(
                    self.run_id, node.node_id, lifecycle.attempt_no
                )
            )
            completed = (
                None
                if output_sha is None
                else store.candidate_review(
                    self.run_id, review_node.node_id, output_sha
                )
            )
            valid = (
                attempt is not None
                and output_sha is not None
                and wt.is_attempt_output_commit(
                    Path(self.deps.repo),
                    output_sha,
                    run_id=self.run_id,
                    node_id=node.node_id,
                    attempt_no=lifecycle.attempt_no,
                    expected_base=attempt.base_sha,
                )
                and completed is not None
                and completed.verdict is st.ReviewVerdict.PASS
                and self._review_is_accepted(review_node.node_id)
            )
            if not valid:
                store.mark_blocked(
                    self.run_id,
                    node.node_id,
                    st.BlockReason.OUTPUT_IDENTITY_INVALID,
                    detail={
                        "reason": (
                            "accepted lane phase lacks the sealed exact PASS "
                            "needed to finish verification"
                        )
                    },
                )
                continue
            store.mark_verified(self.run_id, node.node_id, output_sha)
            self._output_shas[node.node_id] = output_sha

    def _attempt_retains_output(
        self,
        node_id: str,
        attempt_no: int,
        base_sha: str,
        output_sha: str,
    ) -> bool:
        """Whether an attempt still owns an exact or published prior output."""
        if wt.is_attempt_output_commit(
            self.deps.repo,
            output_sha,
            run_id=self.run_id,
            node_id=node_id,
            attempt_no=attempt_no,
            expected_base=base_sha,
        ):
            return True
        return self.deps.store.candidate(
            self.run_id, node_id, output_sha
        ) is not None and wt.is_attempt_lineage_commit(
            self.deps.repo,
            output_sha,
            run_id=self.run_id,
            node_id=node_id,
            attempt_no=attempt_no,
            expected_base=base_sha,
        )

    def project(self) -> None:
        """Write the DAG projection, once (§7.1)."""
        if self._projected:
            return
        try:
            # The store's explicit review-projection API is the only writer of
            # derived rows.  Pass authored nodes here so an interrupted fresh
            # run is resumed through the same idempotent seam as an old run.
            self.deps.store.create_run(
                self.run_id,
                self.plan_digest,
                list(self.authored_nodes.values()),
                plan_name=self.plan_name,
                test_strength_contract=self.plan_test_strength_contract,
            )
        except lc.RunAlreadyExists:
            # An existing run keeps the contract it was created under. The pin
            # is compared, never rewritten: a run resumed by a binary that
            # would derive a different contract from the same plan is a run
            # whose already-terminal nodes were decided under other rules, and
            # continuing it under new ones is exactly the retroactive
            # reclassification the rollout invariant forbids.
            #
            # Asked only of a run that **has** tests nodes. A plan with none
            # derives `STRENGTH_V1` — there is nothing weaker about it — while
            # every run created before the column reads `LEGACY`, so comparing
            # the two unconditionally refuses the resume of every run that
            # already exists, for a contract that governs nothing in it.
            #
            # Compared against what the *plan* implies. `self.test_strength_
            # contract` is already the pin on this path -- comparing it here
            # would compare the pin to itself and never refuse anything.
            pinned = self.deps.store.test_strength_contract(self.run_id)
            governs = any(node.kind is st.NodeKind.TESTS
                          for node in self.authored_nodes.values())
            if governs and pinned is not self.plan_test_strength_contract:
                raise TestStrengthContractMismatch(
                    "{0} was created under the {1} test-acceptance contract "
                    "and this plan implies {2}; a run is pinned to the "
                    "contract it started with. Start a new run rather than "
                    "resuming this one under different rules.".format(
                        self.run_id, pinned.value,
                        self.plan_test_strength_contract.value))
        # Retain the plan bytes on the run, for a fresh run and a resumed one
        # alike. Idempotent on the digest, so a resume re-asserting what it
        # already runs costs nothing.
        #
        # The resumed case is not incidental: a run created before retention
        # existed has no stored bytes, and this is where it acquires them —
        # from the installed file that still matches its digest. That is the
        # only window in which a legacy run becomes amendable, and it closes
        # the moment somebody ships over that file.
        if self.deps.plan_bytes is not None:
            self.deps.store.record_plan_version(
                self.run_id, self.plan_digest, self.deps.plan_bytes
            )
        # This is intentionally also called for a new run.  It either inserts
        # the derived review plus rewired direct downstream edges, or validates
        # the exact durable row a prior scheduler process left behind.
        for review in self._derived_review_nodes():
            self.deps.store.ensure_derived_review_node(
                self.run_id,
                review.review_of,
                depth=review.depth,
                downstream_needs=self._downstream_authored_nodes(review.review_of),
            )
        # This process now owns the run, whether it just projected the plan or
        # adopted an existing projection. Recorded durably because it is the
        # only fact that can later contradict a reader calling a dead run live:
        # `runs.latest_outcome` is written by a scheduler declaring quiescence,
        # so a scheduler that dies before declaring leaves nothing behind that
        # says the run stopped (§7.3, §11.2). Written on the `RunAlreadyExists`
        # path too — that is a second process taking over a run the ledger
        # still attributes to the first.
        self.deps.store.claim_run(self.run_id)
        self._reconcile_accepted_pass_crash()

        # Both durable states carry authority only after their persisted SHA is
        # revalidated. A crash after VERIFIED has no in-memory output map, so
        # excluding it would strand a ready merge forever; trusting an
        # unverified string would let a corrupt row choose the merge input.
        for node_id, node in self.nodes.items():
            # Reviews carry verdict evidence, never a source commit.  Treating
            # their terminal state as an output SHA would both reject a sound
            # resume and let a review enter the merge frontier.
            if self._is_derived_review(node):
                continue
            row = self.deps.store.get_node(self.run_id, node_id)
            if row.state not in (st.NodeState.VERIFIED, st.NodeState.MERGED):
                continue
            output_sha = row.output_sha
            valid = output_sha is not None
            if valid and row.state is st.NodeState.VERIFIED:
                try:
                    attempt = self.deps.store.get_attempt(
                        self.run_id, node_id, row.attempt_no
                    )
                except lc.UnknownNode:
                    valid = False
                else:
                    valid = (
                        attempt.state is st.NodeState.VERIFIED
                        and self._attempt_retains_output(
                            node_id,
                            row.attempt_no,
                            attempt.base_sha,
                            output_sha,
                        )
                    )
            elif valid:
                valid = wt.is_valid_output_commit(
                    Path(self.deps.integration_path), output_sha
                ) and wt.final_ancestry_sweep(
                    Path(self.deps.integration_path), {node_id: output_sha}
                ).get(node_id, False)
            if not valid:
                if row.state is st.NodeState.VERIFIED:
                    self.deps.store.mark_blocked(
                        self.run_id, node_id, st.BlockReason.OUTPUT_IDENTITY_INVALID
                    )
                    continue
                raise DurableOutputIdentityError(
                    f"{self.run_id}/{node_id}: MERGED output identity is invalid"
                )
            self._output_shas[node_id] = output_sha
        # Retry prompts and convergence are both derived state. Legacy runs
        # carry prompt guidance on attempt rows; the persistent correction
        # loop carries each semantic failure in ``lane_retry_spend`` and each
        # review rejection in ``candidate_reviews`` because one retained
        # attempt may publish several candidates. Rebuild both representations
        # here so resume cannot silently forget a standing constraint.
        attempts = self.deps.store.attempts_for(self.run_id)
        self._guidance = rp.guidance_from_attempts(attempts)
        latest_attempts: Dict[str, st.AttemptRecord] = {}
        for attempt in attempts:
            previous = latest_attempts.get(attempt.node_id)
            if previous is None or attempt.attempt_no > previous.attempt_no:
                latest_attempts[attempt.node_id] = attempt
        for build_node_id, attempt in latest_attempts.items():
            if self._review_for_build(build_node_id) is not None:
                self._refresh_lane_guidance(build_node_id, attempt)
        self._projected = True

    def cancel(self) -> None:
        """Latch cancellation; the run loop owns its quiescent completion."""
        with self._lock:
            self._cancelled.set()

    def request_pause(self) -> None:
        """Stop new work and quiesce workers without declaring an outcome.

        `_cancelled` is set too, because it is the flag every stopping path in
        the run loop already reads; `_paused` is what makes the difference
        between the two stops. Nothing durable is written here or by the loop
        it stops — that is the property `run resume` depends on.

        Not for the SIGINT handler: `Event.set` and `_lock` are not
        signal-safe. The handler raises `KeyboardInterrupt`; this method
        runs on the main thread after that interrupt is caught.
        """
        with self._lock:
            self._paused.set()
            self._cancelled.set()

    def _handle_sigint(self, signum, frame) -> None:
        """SIGINT is a pause request; a later SIGINT after pause is armed
        escalates by raising KeyboardInterrupt so a hung wait unblocks.

        `run pause` signals the claiming process rather than writing a row,
        because the process is the thing that has to stop — its `finally`
        releases the integration checkout, and a row could not have done that.
        The handler latches a bool and raises; the loop is what quiesces, so
        no durable state is touched from inside a signal handler. A second
        SIGINT after the loop has armed escalation still writes nothing
        (§1.2) — it only refuses to wait forever for a hung quiesce.
        """
        if self._sigint_escalate_armed:
            self._sigint_escalated = True
            raise SigintInterrupt
        self._sigint_pause_latched = True
        raise SigintInterrupt

    def shutdown(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)

    def _shutdown_escalated(self) -> None:
        """Second SIGINT: do not wait for workers that have already hung."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _preserve_unpublished_work(
        self, attempt: st.AttemptRecord, reason: str = "NODE_TIMEOUT"
    ) -> None:
        """Publish unpublished builder work onto the attempt ref (#97, #110).

        Empty delta is a no-op — nothing to salvage. Errors propagate to the
        caller: the watchdog still fails the attempt, and SIGINT escalate
        still leaves the pause-shaped ledger.
        """
        with self._lock:
            worktree = self._attempt_worktrees.get(attempt.node_id)
        if (
            worktree is None
            or worktree.attempt_no != attempt.attempt_no
            or worktree.baseline is None
        ):
            return
        after = wt.inventory(worktree.path)
        measured = wt.delta(worktree.baseline, after)
        if measured.is_empty:
            return
        wt.commit_measured_delta(
            worktree,
            measured,
            after,
            "{} attempt {} {}".format(attempt.node_id, attempt.attempt_no, reason),
        )

    def _preserve_running_unpublished(self) -> None:
        """PR #124's preserve step, for every RUNNING node, before escalate."""
        for row in self.deps.store.node_records(self.run_id):
            if row.state != st.NodeState.RUNNING.value:
                continue
            lifecycle = self.deps.store.get_node(self.run_id, row.node_id)
            try:
                attempt = self.deps.store.get_attempt(
                    self.run_id, row.node_id, lifecycle.attempt_no
                )
            except lc.UnknownNode:
                continue
            try:
                self._preserve_unpublished_work(attempt, reason="SIGINT_ESCALATED")
            except Exception:
                continue

    def _owns_running(self, record: st.AttemptRecord) -> bool:
        lifecycle = self.deps.store.get_node(self.run_id, record.node_id)
        return (
            lifecycle.state is st.NodeState.RUNNING
            and lifecycle.attempt_no == record.attempt_no
        )

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
                    f"{record.node_id}#{record.attempt_no} was fenced by watchdog"
                )
            if not self._owns_running(record):
                raise AttemptOwnershipLost(
                    f"{record.node_id}#{record.attempt_no} no longer owns RUNNING"
                )

    def _attempt_dispatched(self, record: st.AttemptRecord) -> bool:
        """Whether `run_node` was ever entered for this attempt.

        Structural, and recorded by the frame that knew, at the moment it
        became true (§7.5 permits a classifier a typed fact and forbids it a
        message). `run_node` is the sole opener of the pane or process a
        settle exists to prove absent, so a `False` here is absence by
        construction rather than absence asserted. An attempt this scheduler
        never leased is unknown, not absent, and answers `True`.
        """
        with self._lock:
            return self._attempt_dispatch.get((record.node_id, record.attempt_no), True)

    def _say(self, node_id: str, attempt_no: int, phase: str,
             **detail: object) -> None:
        """Report one pre-dispatch phase to whoever is watching. Never fails.

        The exception swallow is deliberate and narrow: this is the only call
        in an attempt whose whole purpose is to be seen, so a broken terminal,
        a closed pipe, or a reporter someone wired wrong must not take the
        attempt with it. Nothing downstream reads what is printed here.
        """
        reporter = self.deps.progress
        if reporter is None:
            return
        try:
            reporter(node_id, attempt_no, phase, dict(detail))
        except Exception:  # noqa: BLE001 - display must never fail an attempt
            pass

    def _quiesce(
        self,
        record: st.AttemptRecord,
        phase: str,
        in_flight: Optional[BaseException] = None,
    ) -> None:
        """Prove this attempt's owned execution absent, when it could have any.

        `in_flight` is the exception this quiesce is cleaning up after, when
        there is one. It is read for one structural fact and never for its
        message: a launch failure that has *stated* it left nothing to reap
        makes the proof a question about a process group that never existed,
        and the answer to that question must not outrank the failure that
        caused it (§19 M3).
        """
        if _launch_left_nothing_to_reap(in_flight):
            return
        try:
            self.deps.quiesce_attempt(record, phase)
        except (KeyboardInterrupt, SigintInterrupt):
            raise
        except BaseException as exc:
            raise QuiescenceFailure(phase, exc) from exc

    def _settling(self) -> bool:
        """Whether any node's durable row still says RUNNING.

        Called only when no future is in flight, so a RUNNING row here has no
        worker behind it: the worker returned and the verdict that will move
        the node — the watchdog's, or its own settle — has not committed yet.
        That is a node mid-transition, not a quiescent one, and §7.3 defines
        quiescence as "nothing in flight and no node can progress".
        """
        return any(
            record.state == st.NodeState.RUNNING.value
            for record in self.deps.store.node_records(self.run_id)
        )

    def _settle_context(
        self, context: _AttemptContext, in_flight: Optional[BaseException] = None
    ) -> None:
        """§8.3's settle, taken over the execution this attempt dispatched.

        An attempt that never entered `run_node` dispatched none, and every
        pre-dispatch exit has already accounted for the contexts it did use:
        a failed `check_at_create` precedes provision and the pre gate
        entirely, and everything after it leaves through the `finally` that
        takes the `pre-baseline` proof. So the settle there is a question
        about nothing, and the runtime's answer for nothing —
        `PROCESS_GROUP_UNTRACKED` — is terminal, which is how it replaced
        GATE_NOT_FALSIFIABLE, a failed worktree check, and a refused launch
        with QUIESCENCE_UNPROVEN.

        `in_flight` covers the dispatched half of the same shape: a launch
        that failed having stated it left nothing to reap (§19 M3).
        """
        if context.record is None or context.settled:
            return
        if self._attempt_dispatched(context.record):
            self._quiesce(context.record, "settle", in_flight)
        context.settled = True

    def _block_quiescence(
        self,
        node: st.PlanNode,
        record: Optional[st.AttemptRecord],
        failure: QuiescenceFailure,
        *,
        allow_watchdog_fence: bool = False,
    ) -> None:
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
                chain.append("{0}: {1}".format(type(current).__name__, current))
                current = current.__cause__ or current.__context__
            prior_phase = None
            if self._review_for_build(node.node_id) is not None:
                prior_phase = self.deps.store.get_node(
                    self.run_id, node.node_id
                ).lane_phase
                self._set_lane_phase(
                    node.node_id,
                    st.LanePhase.BLOCKED,
                    record,
                    allow_watchdog_fence=(
                        allow_watchdog_fence or self._cancelled.is_set()
                    ),
                )
            self.deps.store.mark_blocked(
                self.run_id,
                node.node_id,
                st.BlockReason.QUIESCENCE_UNPROVEN,
                detail={
                    "phase": failure.phase,
                    "exception_type": type(cause).__name__,
                    "causes": chain[:6],
                },
                attempt_extra=(
                    {lc.LATE_ENVELOPE_PHASE_KEY: prior_phase.value}
                    if prior_phase is not None
                    and prior_phase is not st.LanePhase.BLOCKED
                    else None
                ),
            )

    def _block_unserviceable_handoff(
        self,
        node: st.PlanNode,
        record: Optional[st.AttemptRecord],
        failure: "UnserviceableHandoff",
        *,
        candidate_sha: Optional[str] = None,
        builder_generation: Optional[int] = None,
    ) -> None:
        """Record a repair handoff nothing can deliver as a typed transition.

        §1.2 — the transition keys on the exception's *type* and on the
        lane's own durable rows, never on the message, which is carried only
        as detail an operator can read. The message is diagnosis; the type is
        the decision.

        Idempotent by the same `_owns_running` fence every other terminal
        path uses, so the precise call in `_continue_repair_handoff` and the
        backstop in `_attempt` cannot both write.
        """
        if record is None:
            return
        with self._lock:
            if not self._owns_running(record):
                return
            if candidate_sha:
                self.deps.store.fail_handoff(
                    self.run_id,
                    node.node_id,
                    candidate_sha,
                    builder_generation=(
                        builder_generation
                        if builder_generation is not None
                        else record.attempt_no
                    ),
                    reason=str(failure),
                )
            if self._review_for_build(node.node_id) is not None:
                self._set_lane_phase(
                    node.node_id,
                    st.LanePhase.BLOCKED,
                    record,
                    allow_watchdog_fence=self._cancelled.is_set(),
                )
            detail = {
                "reason": str(failure),
                "exception_type": type(failure).__name__,
                "node_kind": node.kind.value,
            }
            if candidate_sha:
                detail["candidate_sha"] = candidate_sha
            self.deps.store.mark_blocked(
                self.run_id,
                node.node_id,
                st.BlockReason.REPAIR_HANDOFF_UNSERVICEABLE,
                detail=detail,
            )

    def _contain_quiescence_failure(
        self,
        node: st.PlanNode,
        record: Optional[st.AttemptRecord],
        failure: QuiescenceFailure,
        *,
        allow_watchdog_fence: bool = False,
    ) -> None:
        """Contain a failed absence proof unless interruption is a pause.

        Pausing still asks the runtime to interrupt and quiesce the owned
        execution, but it deliberately writes no lifecycle outcome. A failed
        proof during that best-effort interruption is therefore not durable
        evidence that the lane is blocked. Cancellation and every other stop
        path retain the ordinary fail-closed containment below.
        """
        if self._paused.is_set() or self._sigint_pause_latched:
            return
        self._block_quiescence(
            node,
            record,
            failure,
            allow_watchdog_fence=allow_watchdog_fence,
        )

    def _request_cancel(self, node_id: str) -> None:
        lifecycle = self.deps.store.get_node(self.run_id, node_id)
        if lifecycle.state is not st.NodeState.RUNNING:
            return
        try:
            record = self.deps.store.get_attempt(
                self.run_id, node_id, lifecycle.attempt_no
            )
        except lc.UnknownNode:
            return
        try:
            self._quiesce(record, "cancel")
            if (
                self._review_for_build(node_id) is not None
                and self.deps.close_review is not None
            ):
                # Cancellation reaches this point only after absence proof.
                self.deps.close_review(node_id)
        except QuiescenceFailure as exc:
            node = self.nodes.get(node_id)
            if node is not None:
                self._contain_quiescence_failure(node, record, exc)

    def _interrupt_in_flight(
        self, in_flight: Dict[str, "Future"], cancellation_requested: set
    ) -> None:
        """`Future.cancel` cannot stop a running worker. Quiesce it first."""
        for node_id, future in list(in_flight.items()):
            future.cancel()
            if node_id not in cancellation_requested:
                self._request_cancel(node_id)
                cancellation_requested.add(node_id)

    # ── the main loop ───────────────────────────────────────────────────────

    def run(self) -> RunReport:
        """Schedule until quiescence, then declare exactly one outcome.

        Quiescence is "nothing in flight and no node can progress" — not "no
        pending nodes", because a node stranded behind a blocked ancestor
        stays PENDING forever and is exactly the shape the run must stop on.

        One exit does not declare: a pause. `run pause` SIGINTs this process,
        `_handle_sigint` raises `KeyboardInterrupt`, the loop latches
        `_paused`, and the loop stops the same way a cancel stops it —
        except that nothing durable is written and the exit raises
        `RunPaused` instead. The handler is installed for the duration of
        the loop and the previous one restored on every path out, including
        the exceptional ones, so a scheduler embedded in a longer-lived
        process does not leave its SIGINT behind. Installing it can fail
        outright off the main thread; that is printed as
        `SIGINT_HANDLER_NOT_INSTALLED` rather than swallowed, and a
        scheduler that cannot receive the signal simply cannot be paused by
        it.
        """
        previous_sigint = None
        try:
            previous_sigint = signal.signal(signal.SIGINT, self._handle_sigint)
            self._sigint_handler_installed = True
        except ValueError as exc:
            previous_sigint = None
            self._sigint_handler_installed = False
            print("SIGINT_HANDLER_NOT_INSTALLED: {0}".format(exc), file=sys.stderr)
        self.project()
        if self._sigint_pause_latched:
            self.request_pause()
        if self._paused.is_set():
            return self._finish_paused(previous_sigint)
        if self._cancelled.is_set():
            self.deps.store.cancel_run(self.run_id)
            return self._finish(self._declare(), previous_sigint)

        self._pool = ThreadPoolExecutor(max_workers=self.config.concurrency)
        in_flight: Dict[str, "Future"] = {}
        cancellation_requested = set()
        watchdog, backstop = self._start_liveness()
        watchdog.start()
        try:
            while True:
                try:
                    if self._cancelled.is_set():
                        # A Future cannot interrupt a running worker. Quiesce its
                        # owned execution first, then wait for the worker to stop
                        # observing its RUNNING lease before cancelling durable
                        # state. Otherwise a late worker could commit into a retry.
                        # Arm escalation only here, after the loop has observed
                        # the pause: a single Ctrl-C under `uv run` delivers
                        # SIGINT twice, and that burst must not escalate.
                        if self._paused.is_set() or self._sigint_pause_latched:
                            self._sigint_escalate_armed = True
                        self._interrupt_in_flight(in_flight, cancellation_requested)
                        if in_flight:
                            done, _ = _wait_any(list(in_flight.items()))
                            for node_id in done:
                                in_flight.pop(node_id, None)
                            continue
                        # A pause takes this same path to quiesce, and writes
                        # nothing on the way out: `cancel_run` is a lifecycle
                        # transition, and a pause makes none (§1.2).
                        if not self._paused.is_set():
                            self.deps.store.cancel_run(self.run_id)
                        break

                    if backstop.check():
                        # §11.2 — the backstop's domain is the run's stopping
                        # point, not quiescence: both hang shapes it exists for
                        # have something in flight and nothing transitioning, so
                        # STUCK is declared about a run that still has workers.
                        #
                        # It still has to *stop* those workers, and `Future.cancel`
                        # does not: a running worker is not cancellable, and the
                        # `finally` below already waits on the pool. Cancelling
                        # only the futures therefore left the run blocked in
                        # `shutdown(wait=True)` until each worker reached its own
                        # node timeout — the backstop fired and nothing stopped.
                        # Quiescing owned execution first is what makes the
                        # workers return; the outcome is STUCK either way.
                        self._stuck = True
                        self._interrupt_in_flight(in_flight, cancellation_requested)
                        if in_flight:
                            done, _ = _wait_any(list(in_flight.items()))
                            for node_id in done:
                                in_flight.pop(node_id, None)
                            continue
                        break

                    self._merge_frontier()
                    if self._cancelled.is_set():
                        continue
                    ready = [
                        node_id
                        for node_id in self._runnable_ready_nodes()
                        if node_id not in in_flight
                    ]
                    for node_id in ready:
                        if (
                            self._cancelled.is_set()
                            or len(in_flight) >= self.config.concurrency
                        ):
                            break
                        in_flight[node_id] = self._pool.submit(self._attempt, node_id)

                    if not in_flight:
                        # Nothing running. If the frontier produced nothing to
                        # start either, no node can progress and the run is
                        # quiescent.
                        self._merge_frontier()
                        if self._runnable_ready_nodes():
                            continue
                        if self._settling():
                            # Not quiescent — mid-verdict. `ready_nodes` returns
                            # only PENDING nodes, so a node whose durable row still
                            # says RUNNING while no worker holds it is invisible to
                            # the readiness check AND to the blocked set. The loop
                            # therefore declared the residual BLOCKED naming
                            # nothing: `{"blocked": [], "merged": [], "outcome":
                            # "BLOCKED"}`, with the node still PENDING at attempt 2
                            # and no block_reason — a run that stops with work left
                            # to do and reports it as a mystery. Reproduced 8 times
                            # in 40 whenever a worker returned before the
                            # watchdog's verdict committed.
                            #
                            # Waiting is bounded rather than open-ended: if no
                            # verdict ever lands, §11.2's backstop declares STUCK,
                            # which is the truthful answer for a run that stopped
                            # with something in flight. The wait is on the
                            # cancellation event so a cancel stays responsive.
                            self._cancelled.wait(_SETTLING_POLL_S)
                            continue
                        break

                    done, _ = _wait_any(list(in_flight.items()))
                    for node_id in done:
                        in_flight.pop(node_id, None)
                except (KeyboardInterrupt, SigintInterrupt):
                    if self._sigint_escalated:
                        self.request_pause()
                        break
                    self.request_pause()
                    continue
        finally:
            watchdog.stop()
            if self._sigint_escalated:
                self._preserve_running_unpublished()
                self._shutdown_escalated()
            else:
                self.shutdown()
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)

        if self._paused.is_set() or self._sigint_pause_latched:
            raise RunPaused(self.run_id)
        return self._declare()

    def _finish(self, report: RunReport, previous_sigint) -> RunReport:
        """Restore the caller's SIGINT handler, then hand back the report.

        The two early returns above leave before the `try`/`finally` that
        restores it, so they restore it here instead.
        """
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)
        return report

    def _finish_paused(self, previous_sigint) -> RunReport:
        """The same, for the early exit that has no report to hand back."""
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)
        raise RunPaused(self.run_id)

    def _start_liveness(self):
        """Start §7.6's single watchdog thread and §11.2's run-level timer.

        Two clocks have to be made to agree here, and getting it wrong is
        silent in the worst direction. `RunBackstop` defaults its time source
        to `time.monotonic`, while the store's `last_transition_at` returns
        epoch seconds — subtracting one from the other yields a number with no
        meaning, and on this machine a large negative one, so the backstop
        would simply never fire. Both timers therefore read `self._time_source`,
        which production defaults to `time.time`, matching the column they read.
        """
        store = self.deps.store

        def running_attempts():
            return [
                a
                for a in store.attempts_for(self.run_id)
                if a.state is st.NodeState.RUNNING
            ]

        def kill(attempt):
            # `_stall` calls the killer before `fail_attempt`. Revoke the
            # worker's authority here, rather than after the potentially
            # blocking quiescence proof in `fail`, so no resumed provisioner
            # can pass its next generation boundary in that interval.
            self._fence_watchdog_generation(attempt)
            if self.deps.kill_attempt is None:
                return
            if not self._attempt_dispatched(attempt):
                return
            try:
                self.deps.kill_attempt(attempt)
            except BaseException as exc:
                node = self.nodes.get(attempt.node_id)
                if node is not None:
                    self._contain_quiescence_failure(
                        node,
                        attempt,
                        QuiescenceFailure("watchdog-kill", exc),
                        allow_watchdog_fence=True,
                    )

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
                if self._attempt_dispatched(attempt):
                    self._quiesce(attempt, "watchdog")
            except QuiescenceFailure as exc:
                self._contain_quiescence_failure(
                    node, attempt, exc, allow_watchdog_fence=True
                )
                return
            if not self._cancelled.is_set():
                self._settle_failure(
                    node,
                    rp.Classification(retry_class=retry_class, reason=reason),
                    record=attempt,
                    allow_watchdog_fence=True,
                )

        def exit_status_observed(attempt: st.AttemptRecord) -> bool:
            """Whether something other than the watchdog sees this process end.

            §9.7: an artifact a worker wrote outranks any status a supervisor
            observes about that worker, and absence of a process is not
            absence of output. The watchdog is the supervisor here, and this
            is the fact that tells it when it is not the component entitled to
            rule — which depends on who holds the process handle, so the
            scheduler answers it rather than the watchdog guessing.

            A **code** node's command is started by the harness itself, as the
            leader of its own process group (§8.3), and the runner polls that
            handle until it exits and returns the exit code the verification
            predicate then reads (§7.3). Its exit is fully accounted for, and
            a healthy fast command — the measured case exits in milliseconds —
            is otherwise convicted PROCESS_DEAD purely for finishing between
            two polls.

            An **agent** node's process is spawned by the herdr server. §8.3
            is explicit that herdr's recorded surface exposes no pid and no
            process group, so Maestro holds no handle and nothing else can see
            that process end: absence really is the only signal, which is the
            case §7.6 wrote the signal for, and it keeps its full force.

            Worth recording because it inverts what the code looked like: the
            signal was **unreachable** for the kind it was written for — an
            agent attempt's `pid` is `handle.process_group`, which §16.3 item
            17 establishes is never populated — and **armed** for the one kind
            whose completion the harness already observes. It fired only where
            it was wrong.
            """
            node = self.nodes.get(attempt.node_id)
            return node is not None and node.kind is st.NodeKind.CODE

        def declared_result_observed(attempt: st.AttemptRecord) -> bool:
            """Whether this exact attempt already landed an adjudicated result.

            §9.7 again, at the two clock signals rather than the process one,
            and for the failure `exit_status_observed` does not cover: the
            attempt is not gone, it is *finished*. `_attempt` quiesces a
            successful builder at `candidate-idle` — before it measures and
            publishes the candidate, before the reviewer is dispatched — so
            the builder's transcript cannot grow again from that point by
            construction. The watchdog goes on measuring exactly that file for
            the whole of the review, and review latency tracks reviewer turn
            count, so it is unbounded.

            Measured on run-9e9ac412669140039ae078601048f6c7: of ten reviews
            spanning 46s to 461s, both that exceeded `turn_timeout_s=300` were
            failed ENVIRONMENTAL while holding committed work and an
            adjudicated result row — an infra retry debited for what came back
            as a review rejection seconds later, the reviewer's findings
            discarded, the relaunch blind, the review burned.

            The fact is structural and among those §7.5 permits: whether a
            typed `results` row exists for `(run_id, node_id, attempt_no)` and
            what its `adjudication` enum says. `result_adjudication` does not
            select the payload, so §1.2 holds by projection rather than by
            restraint — no transition here can be caused by envelope prose.

            ACCEPTED alone, because that is the verdict `adjudicate_result`
            reaches only when the result names *this* attempt, at *this* base,
            while it was still the live one (§7.7). The other three each say
            the row describes some other generation's work and must not spare
            this one.
            """
            return (
                self.deps.store.result_adjudication(
                    self.run_id, attempt.node_id, attempt.attempt_no
                )
                is st.Adjudication.ACCEPTED
            )

        def preserve_unpublished(attempt: st.AttemptRecord) -> None:
            """Publish unpublished builder work onto the attempt ref (#97).

            Kill already ran. Empty delta is a no-op — nothing to salvage.
            Errors propagate to the watchdog's preserve wrapper, which still
            fails the attempt.
            """
            self._preserve_unpublished_work(attempt)

        watchdog = wd.Watchdog(
            config=self.config,
            attempts_provider=running_attempts,
            write_heartbeat=store.record_heartbeat,
            kill=kill,
            fail_attempt=fail,
            exit_status_observed=exit_status_observed,
            declared_result_observed=declared_result_observed,
            actor_status=self.deps.actor_status,
            preserve_unpublished=preserve_unpublished,
            time_source=self._time_source,
        )

        backstop = wd.RunBackstop(
            config=self.config,
            last_transition_at=lambda: store.last_transition_at(self.run_id),
            time_source=self._time_source,
        )
        return watchdog, backstop


    # ── one attempt (§7.3, §7.4, §8.3, §8.4) ────────────────────────────────

    def _attempt(self, node_id: str) -> None:
        """Run one leased attempt and never classify a superseded generation."""
        node = self.nodes[node_id]
        context = _AttemptContext()
        try:
            self._attempt_body(node, context)
        except UnserviceableHandoff as exc:
            # Caught *before* the ownership clause below, and that ordering is
            # the fix. `UnserviceableHandoff` subclasses `AttemptOwnershipLost`
            # so it survives every deliberate re-raise on the way here, and
            # without this clause it landed in that bare `return`: the lane
            # simply stopped existing, with `attempts` and `node_lifecycle`
            # still reading RUNNING and the ledger's last row being the
            # REPAIRING phase it had just entered. A handoff nothing can
            # deliver is a terminal fact and gets a typed transition
            # (run-36dd33d262d9485ca815aea5001b2ce2, `lane-wp6-tests`).
            try:
                self._settle_context(context)
            except QuiescenceFailure as quiescence:
                self._contain_quiescence_failure(node, context.record, quiescence)
                return
            self._block_unserviceable_handoff(node, context.record, exc)
            return
        except (AttemptCancelled, AttemptOwnershipLost):
            # Even a superseded/cancelled worker must prove its latest boundary
            # quiescent before returning control to the scheduler loop. A
            # genuine cancel writes no outcome here and must not start doing
            # so: the run-level cancel path owns that record.
            try:
                self._settle_context(context)
            except QuiescenceFailure as exc:
                self._contain_quiescence_failure(node, context.record, exc)
            return
        except QuiescenceFailure as exc:
            self._contain_quiescence_failure(node, context.record, exc)
        except wt.HarnessQuiescenceError as exc:
            self._contain_quiescence_failure(
                node, context.record, QuiescenceFailure("harness-gate", exc)
            )
        except DurableOutputIdentityError as exc:
            # A recovery row that cannot prove its durable output identity is
            # not permission to mint another attempt. The retained worktree
            # and generation are the evidence that needs repair; replacing
            # them repeats authored work and can race the still-finishing
            # builder a second time.
            try:
                self._settle_context(context, in_flight=exc)
            except QuiescenceFailure as quiescence:
                self._contain_quiescence_failure(node, context.record, quiescence)
                return
            if (
                context.record is not None
                and not self._cancelled.is_set()
                and self._owns_running(context.record)
            ):
                self._settle_failure(
                    node,
                    rp.Classification(
                        block_reason=st.BlockReason.OUTPUT_IDENTITY_INVALID,
                        reason=_exception_reason(exc),
                    ),
                    record=context.record,
                )
        except BaseException as exc:  # noqa: BLE001 — containment is the point
            try:
                # `exc` is handed to the settle, not merely caught around it.
                # §19 M3's shape one frame up: a launch that failed having
                # stated it left nothing to reap must not then be asked to
                # prove a process group absent, because the quiescer answers
                # `PROCESS_GROUP_UNTRACKED` about a group that never existed
                # and that answer is terminal. Fixed at the `pre-inventory`
                # site and not here, so the same refusal crossed a second,
                # unguarded proof on its way out and blocked
                # QUIESCENCE_UNPROVEN anyway — run-2a44d226e75a4be391a14f02b78a6d25,
                # `lane-p1-freeze-and-run-log#4`, at zero turns.
                self._settle_context(context, in_flight=exc)
            except QuiescenceFailure as quiescence:
                self._contain_quiescence_failure(node, context.record, quiescence)
                return
            if (
                context.record is not None
                and not self._cancelled.is_set()
                and self._owns_running(context.record)
            ):
                # A `LaunchFailed` names its own class in a typed enum member,
                # so `classify`'s launcher branch is reachable from the real
                # adapter for the first time (§16.3 item 42). Every other
                # exception still falls to the ENVIRONMENTAL default,
                # unchanged and fail-closed.
                # `classify_with_containment` rather than bare `classify`:
                # §7.5 requires that *any* exception reaching a worker's
                # top-level handler without a classification default to
                # ENVIRONMENTAL, and a build that raises while turning a raw
                # failure into a signal is precisely an engine bug rather than
                # a fact about the code under test. Bare `classify` left that
                # to the shape of the build happening not to raise, which is a
                # property of today's fields rather than an invariant.
                #
                # `classified_failure`, not `failure`: a refusal the launcher
                # typed as deterministic is upgraded to the zero-budget member
                # so it blocks on its first occurrence naming the refusal,
                # rather than spending two launches that cannot differ (§16.3
                # item 46).
                exception_type = type(exc).__name__
                launcher_failure = (
                    exc.classified_failure if isinstance(exc, LaunchFailed) else None
                )

                def build_signal() -> rp.FailureSignal:
                    return rp.FailureSignal(
                        node_kind=node.kind,
                        exception_type=exception_type,
                        launcher_failure=launcher_failure,
                    )

                # `classify` reads none of the signal's ENVIRONMENTAL evidence
                # by design (§7.5 forbids the lexical shortcut), so its
                # fall-through returns a classification with no account of
                # itself and the row lands empty. The exception is what this
                # arm observed, and the launcher's typed vocabulary --
                # LAUNCH_REFUSED, AGENT_GONE, ENVELOPE_UNPARSED -- travels in
                # it. Recorded the way `_block_quiescence` records a cause:
                # type and message, written and never read back (§10.1).
                self._settle_failure(
                    node,
                    _with_reason(
                        rp.classify_with_containment(build_signal),
                        _exception_reason(exc),
                    ),
                    record=context.record,
                )

    def _durable_repair_resume(
        self,
        node: st.PlanNode,
        lifecycle: st.NodeLifecycle,
    ) -> Tuple[
        Optional[rp.RepairBasis],
        Optional[st.CandidateReview],
        Optional[st.RepairHandoff],
    ]:
        """Recover a rejected candidate and its handoff for a new generation."""
        if lifecycle.lane_phase not in (
            st.LanePhase.REPAIR_HANDOFF,
            st.LanePhase.REPAIRING,
            st.LanePhase.WAITING_FOR_NEW_CANDIDATE,
        ):
            return None, None, None
        review_node = self._review_for_build(node.node_id)
        if review_node is None or not lifecycle.attempt_no:
            return None, None, None
        candidates = self.deps.store.lane_candidates(
            self.run_id, node.node_id, limit=10_000
        )
        if not candidates:
            return None, None, None
        candidate = candidates[-1]
        review = self.deps.store.candidate_review(
            self.run_id, review_node.node_id, candidate.candidate_sha
        )
        handoff = self.deps.store.repair_handoff(
            self.run_id, node.node_id, candidate.candidate_sha
        )
        if (
            review is None
            or review.verdict is not st.ReviewVerdict.REJECTED
            or handoff is None
        ):
            return None, None, None
        rejected_attempt = self.deps.store.get_attempt(
            self.run_id, node.node_id, lifecycle.attempt_no
        )
        return (
            rp.RepairBasis(
                base_sha=candidate.candidate_sha,
                integration_head=rejected_attempt.integration_head,
                repair_of_attempt=lifecycle.attempt_no,
                chain_length=candidate.candidate_seq,
            ),
            review,
            handoff,
        )

    def _attempt_body(self, node: st.PlanNode, context: _AttemptContext) -> None:
        store = self.deps.store
        existing: Optional[st.AttemptRecord] = None
        lifecycle = store.get_node(self.run_id, node.node_id)
        if lifecycle.attempt_no:
            existing = store.get_attempt(
                self.run_id, node.node_id, lifecycle.attempt_no
            )
            if existing.extra.get(lc.LATE_ENVELOPE_RECOVERY_KEY) is True:
                self._recover_attempt_body(node, context, existing)
                return
        #: A `QUIESCENCE_UNPROVEN` block the resume boundary proved was written
        #: over an attempt that never crossed dispatch. Both halves of that
        #: proof are already durable by the time this reads the marker -- the
        #: ledger's (`undispatched_quiescence_attempts`) and the runtime's, over
        #: the paths only it can see -- so what is left here is not a decision
        #: but its consequence: this generation is redispatched rather than
        #: replaced. `_recover_attempt_body` is the wrong neighbour to reuse for
        #: it; that one continues a builder that already produced something,
        #: and this attempt produced nothing, which is precisely why it may run.
        undispatched = (
            existing is not None
            and existing.extra.get(lc.UNDISPATCHED_RESUME_KEY) is True
        )

        # A process failure may end the persistent scheduler while an immutable
        # candidate and rejected-review handoff remain inside this attempt.
        # Those ledgers are authority to reopen the same attempt/worktree, not
        # permission to mint a replacement generation from integration HEAD.
        basis, durable_repair, durable_handoff = self._durable_repair_resume(
            node, lifecycle
        )
        if basis is not None and existing is not None:
            review_node = self._review_for_build(node.node_id)
            if review_node is None:
                raise lc.LifecycleError(
                    f"{node.node_id}: rejected candidate has no review node"
                )
            store.claim_repair_handoff_attempt(
                self.run_id,
                node.node_id,
                review_node.node_id,
                existing.attempt_no,
                basis.base_sha,
            )
            recovered = store.get_attempt(
                self.run_id, node.node_id, existing.attempt_no
            )
            self._recover_attempt_body(node, context, recovered, already_claimed=True)
            return
        if undispatched and existing is not None:
            # Its own recorded base, never the current integration head. The
            # attempt is being continued, and an attempt's base is the one
            # §7.6 opened its window on; re-deriving it here would silently
            # rebase the same generation onto whatever merged since.
            head = existing.integration_head
            base = existing.base_sha
            attempt_no = store.claim_undispatched_attempt(
                self.run_id, node.node_id, existing.attempt_no
            )
        else:
            head = (
                basis.integration_head
                if basis is not None
                else wt.integration_head(self.deps.repo, self.deps.integration_branch)
            )
            base = basis.base_sha if basis is not None else head
            if basis is None:
                # A published candidate outlives the attempt that produced it,
                # and `publish_candidate` requires every later candidate to be
                # a proven descendant of the last one. `_durable_repair_resume`
                # supplies a basis only for a candidate a reviewer REJECTED; a
                # candidate whose review never started -- an ENVIRONMENTAL
                # retry taken between publication and the reviewer's first
                # turn -- leaves none. Basing that retry on integration HEAD
                # mints a sibling of the published candidate, and the descent
                # assertion then refuses it on every attempt for the life of
                # the run: the lane rebuilds forever and is never reviewed.
                # The published candidate is the lane's real tip, so continue
                # from it rather than from HEAD.
                published = store.lane_candidates(
                    self.run_id, node.node_id, limit=10_000
                )
                if published:
                    base = published[-1].candidate_sha

            # §7.6 — the window opens BEFORE the worktree exists, so a hung
            # `git worktree add` is inside it rather than outside.
            attempt_no = store.start_attempt(
                self.run_id,
                node.node_id,
                base,
                attempt_extra=(
                    rp.repair_extra(basis) if basis is not None else None
                ),
                detail={
                    "repair": (
                        "durable-rejected-candidate"
                        if basis is not None
                        else "fresh-attempt"
                    )
                },
            )
        with self._lock:
            # Recorded against the durable row as soon as it exists, and
            # before anything can observe the attempt RUNNING: leased by this
            # scheduler and not yet dispatched, so every quiescer this attempt
            # meets from here until `run_node` is entered has an owned
            # execution to account for, and that execution is none.
            self._attempt_dispatch.setdefault((node.node_id, attempt_no), False)
        record = store.get_attempt(self.run_id, node.node_id, attempt_no)
        context.record = record
        self._require_running(record)
        if self._review_for_build(node.node_id) is not None:
            self._set_lane_phase(
                node.node_id,
                (
                    st.LanePhase.REPAIRING
                    if durable_repair is not None
                    else st.LanePhase.BUILDING
                ),
                record,
            )

        # The same generation keeps the same checkout when §8.8's cleanup has
        # not already taken it. Reopening is not an optimisation: the branch
        # `create_attempt_worktree` would cut already exists under that name,
        # and its collision guard is the branch creation itself.
        attempt = (
            wt.reopen_attempt_worktree(
                self.deps.repo,
                self.run_id,
                node.node_id,
                attempt_no,
                base,
                Path(self.deps.worktrees_root),
                Path(self.deps.scratch_root),
            )
            if undispatched
            and wt.attempt_worktree_exists(
                Path(self.deps.worktrees_root),
                self.run_id,
                node.node_id,
                attempt_no,
            )
            else wt.create_attempt_worktree(
                self.deps.repo,
                self.run_id,
                node.node_id,
                attempt_no,
                base,
                Path(self.deps.worktrees_root),
                Path(self.deps.scratch_root),
            )
        )
        with self._lock:
            self._attempt_worktrees[node.node_id] = attempt
        self._require_running(record)

        created = wt.check_at_create(attempt)
        self._require_running(record)
        if not created.ok:
            self._settle_context(context)
            # `CheckResult.detail` already names which of §8.3's checks failed
            # and on what; discarding it here left the third ENVIRONMENTAL arm
            # writing the same empty row as the other two.
            self._settle_failure(
                node,
                rp.Classification(
                    retry_class=st.RetryClass.ENVIRONMENTAL,
                    reason=_check_result_reason(created),
                ),
                record=record,
            )
            return

        pre_verdict = None
        try:
            if self.deps.provision is not None:
                self._say(node.node_id, attempt_no, "provisioning",
                          worktree=str(attempt.path))
                provision_started = time.monotonic()
                self.deps.provision(attempt.path)
                self._say(node.node_id, attempt_no, "provisioned",
                          seconds=round(time.monotonic() - provision_started, 1))
            # A slow provision may finish after a watchdog revoked this
            # generation. Its return is a fence: no stale worker reaches a
            # gate, runner, inventory, or commit.
            self._require_running(record)
            if node.kind is st.NodeKind.AGENT:
                self._require_running(record)
                self._say(node.node_id, attempt_no, "pre-gate",
                          command=" ".join(node.gate_command))
                gate_started = time.monotonic()
                pre = self.deps.run_gate(attempt, node, "pre", self._cancelled.is_set)
                self._say(node.node_id, attempt_no, "pre-gate-done",
                          exit_code=pre.exit_code,
                          seconds=round(time.monotonic() - gate_started, 1))
                # A selector path this node is declared to produce, absent
                # at its base, is the red clause 2 asks for -- not a broken
                # runner. Asked per path, because the runner refuses to
                # collect the *whole* selector when any one path is missing:
                # a two-path selector whose first file already exists still
                # exits 4 with no counts. Asked of the joined string, as it
                # was, the test could only ever match a single-path selector,
                # so every multi-path node read as ENVIRONMENTAL and retried
                # an identically absent file until its budget was gone.
                declared = {posixpath.normpath(path) for path in node.outputs}
                unbuilt = any(
                    path in declared and not (attempt.path / path).exists()
                    for path in pm.selector_string_paths(node.gate_selector or "")
                )
                pre_verdict = vf.adjudicate_pre_gate(
                    pre, node.gate_min_cases, selector_unbuilt=unbuilt
                )
        finally:
            # Provision and the pre gate both execute before the measurement
            # bracket; their process groups must be absent before its baseline.
            self._quiesce(record, "pre-baseline")
        self._require_running(record)

        # §7.4 clause 2, asked of the predicate rather than re-derived here.
        # A repair attempt's base is the rejected diff, whose post-gate had to
        # have PASSED for review to run on it at all, so its pre-gate is green
        # by construction; the witness clause 2 wants was taken at the chain
        # root's base, which `basis.integration_head` records.
        if pre_verdict is not None and vf.pre_gate_not_falsifiable(
            pre_verdict, repairing=basis is not None
        ):
            self._settle_context(context)
            self._settle_failure(
                node,
                rp.Classification(block_reason=st.BlockReason.GATE_NOT_FALSIFIABLE),
                record=record,
            )
            return

        self._say(node.node_id, attempt_no, "baseline")
        baseline_started = time.monotonic()
        baseline = wt.take_baseline(attempt)
        self._say(node.node_id, attempt_no, "baseline-done",
                  paths=len(baseline),
                  seconds=round(time.monotonic() - baseline_started, 1))
        # The bracket's before-side, written down while it still exists.
        # `take_baseline` walks the *provisioned* tree, which includes paths
        # git does not track and no commit holds. Once this process is gone
        # that walk is unreproducible: re-deriving the baseline from the base
        # commit sees tracked paths only, so every provisioned untracked path
        # reads as content the attempt added. Anything that measures this
        # attempt after the fact — `attempt salvage` today — reads this row,
        # and refuses when it is absent rather than reconstructing it.
        #
        # `ignored_at_base` travels with it for the same reason and is not the
        # same fact: the baseline's universe is `git ls-files --cached --others
        # --exclude-standard`, and this map is exactly what that command
        # excludes, so no amount of the baseline reconstructs it. Without it
        # `existing_ignored_outputs` has no before-side after the attempt dies,
        # and salvage could commit a node whose declared output is gitignored
        # while the receipt asserts a digest over what was committed (#67).
        store.record_baseline(
            self.run_id,
            node.node_id,
            attempt_no,
            baseline,
            ignored_at_base=attempt.ignored_at_base,
        )
        self._require_running(record)

        def on_launch(
            pid: Optional[int] = None, launched_at: Optional[float] = None
        ) -> None:
            """Arm liveness only while this exact generation still owns RUNNING.

            `launched_at` is passed by a runner that already armed the attempt
            earlier in its own launch — the agent runner writes pane identity
            the instant herdr reports the session path, well before the prompt
            proof finishes. Re-asserting the row here must not move the instant
            the attempt became live forward to whenever the launch happened to
            return; one launch has one `launched_at`. `None` means this call is
            the first, and `mark_launched` stamps the current time.

            It is passed rather than made write-once in the store: a resumed
            generation relaunches under the same attempt row and must be able
            to re-arm, so the row cannot refuse a second `launched_at`.
            """
            with self._lock:
                self._require_running(record)
                store.mark_launched(
                    self.run_id, node.node_id, attempt_no, pid, launched_at
                )

        self._require_running(record)
        with self._lock:
            # The one call that can open a pane or start a process for this
            # attempt. Marked before it is entered, never after: a launch that
            # raises still leaves whatever it created behind, and the quiescer
            # must go on demanding a measured absence for it.
            self._attempt_dispatch[(record.node_id, record.attempt_no)] = True
        self._say(node.node_id, record.attempt_no, "launching")
        guidance = rp.render_guidance(
            node, self._guidance.get(record.guidance_key), repair=basis
        )
        if (
            durable_repair is not None
            and durable_handoff is not None
            and basis is not None
        ):
            if guidance is None:
                guidance = self._repair_prompt(basis.base_sha, durable_repair)
            repair = self._continue_repair_handoff(
                node,
                context,
                attempt,
                record,
                guidance,
                basis.base_sha,
                durable_handoff.builder_generation,
            )
            if repair is None:
                return
            execution = repair.execution
        else:
            try:
                execution = self.deps.run_node(
                    attempt, node, record, guidance, on_launch, self._cancelled.is_set
                )
            except BaseException as exc:
                # Typed launch refusal can prove that no process was created;
                # every other failure still crosses the ordinary absence gate.
                self._quiesce(record, "pre-inventory", in_flight=exc)
                raise
            else:
                self._quiesce(record, "candidate-idle")
        self._complete_attempt(
            node,
            context,
            attempt,
            record,
            basis,
            pre_verdict,
            baseline,
            execution,
            head,
        )

    def _recover_attempt_body(
        self,
        node: st.PlanNode,
        context: _AttemptContext,
        stranded: st.AttemptRecord,
        *,
        already_claimed: bool = False,
    ) -> None:
        """Continue one declared generation without relaunching its builder."""
        store = self.deps.store
        if not already_claimed:
            store.claim_late_envelope_attempt(
                self.run_id, node.node_id, stranded.attempt_no
            )
        record = store.get_attempt(self.run_id, node.node_id, stranded.attempt_no)
        context.record = record
        with self._lock:
            self._attempt_dispatch[(node.node_id, record.attempt_no)] = False
        self._require_running(record)

        if not already_claimed and self.deps.recover_node is None:
            raise DurableOutputIdentityError(
                f"{stranded.node_id}#{stranded.attempt_no}: no late-envelope reader"
            )
        attempt = wt.reopen_attempt_worktree(
            self.deps.repo,
            self.run_id,
            node.node_id,
            stranded.attempt_no,
            stranded.base_sha,
            Path(self.deps.worktrees_root),
            Path(self.deps.scratch_root),
        )
        identity = wt.check_at_create(attempt)
        if not identity.ok:
            raise DurableOutputIdentityError(
                f"{stranded.node_id}#{stranded.attempt_no}: "
                + "; ".join(identity.detail)
            )
        sealed_output_sha = store.attempt_sealed_output(
            self.run_id, node.node_id, stranded.attempt_no
        )
        if sealed_output_sha is not None and not self._attempt_retains_output(
            node.node_id,
            stranded.attempt_no,
            stranded.base_sha,
            sealed_output_sha,
        ):
            raise DurableOutputIdentityError(
                f"{stranded.node_id}#{stranded.attempt_no}: "
                f"sealed output {sealed_output_sha} is neither the attempt "
                "ref nor a published ancestor retained by it"
            )
        baseline = store.attempt_baseline(
            self.run_id, node.node_id, stranded.attempt_no
        )
        ignored = store.attempt_ignored_at_base(
            self.run_id, node.node_id, stranded.attempt_no
        )
        if ignored is None:
            raise lc.BaselineUnrecorded(
                f"{self.run_id}/{node.node_id}#{stranded.attempt_no}: "
                "ignored-at-base evidence was not recorded"
            )
        repair_parent = record.extra.get(lc.REPAIR_HANDOFF_RECOVERY_KEY)
        repair_handoff = None
        unsealed_repair = False
        if sealed_output_sha is not None and repair_parent == sealed_output_sha:
            attempt_tip = wt.attempt_ref_commit(
                self.deps.repo,
                self.run_id,
                node.node_id,
                stranded.attempt_no,
            )
            if attempt_tip == sealed_output_sha:
                baseline = wt.recover_unsealed_descendant(
                    attempt, sealed_output_sha, baseline
                )
                unsealed_repair = True
            else:
                baseline, sealed_output_sha = wt.recover_sealed_descendant(
                    attempt, sealed_output_sha, baseline
                )
                store.record_sealed_output(
                    self.run_id,
                    node.node_id,
                    stranded.attempt_no,
                    sealed_output_sha,
                )
                repair_handoff = store.repair_handoff(
                    self.run_id, node.node_id, repair_parent
                )
                if repair_handoff is None:
                    raise DurableOutputIdentityError(
                        f"{stranded.node_id}#{stranded.attempt_no}: "
                        "recovered repair descendant has no matching handoff"
                    )
                submitted = store.mark_handoff_submitted(
                    self.run_id,
                    node.node_id,
                    repair_parent,
                    builder_generation=repair_handoff.builder_generation,
                )
                acknowledged = store.acknowledge_handoff(
                    self.run_id,
                    node.node_id,
                    repair_parent,
                    builder_generation=repair_handoff.builder_generation,
                )
                if not submitted.submitted or not acknowledged.acknowledged:
                    raise DurableOutputIdentityError(
                        f"{stranded.node_id}#{stranded.attempt_no}: "
                        "recovered repair descendant could not reconcile its handoff"
                    )
        attempt.baseline = baseline
        attempt.ignored_at_base = ignored
        with self._lock:
            self._attempt_worktrees[node.node_id] = attempt

        if unsealed_repair:
            repair_handoff = store.repair_handoff(
                self.run_id, node.node_id, str(repair_parent)
            )
            candidate = store.candidate(self.run_id, node.node_id, str(repair_parent))
            if repair_handoff is None or candidate is None:
                raise DurableOutputIdentityError(
                    f"{stranded.node_id}#{stranded.attempt_no}: "
                    "unsealed repair recovery has no matching candidate and handoff"
                )
            basis = rp.RepairBasis(
                base_sha=str(repair_parent),
                integration_head=record.integration_head,
                repair_of_attempt=record.attempt_no,
                chain_length=candidate.candidate_seq + 1,
            )
            guidance = self._refresh_lane_guidance(node.node_id, record)
            repair_prompt = rp.render_guidance(node, guidance, repair=basis)
            if repair_prompt is None:
                repair_prompt = self._repair_prompt(str(repair_parent), repair_handoff)
            repair = self._continue_repair_handoff(
                node,
                context,
                attempt,
                record,
                repair_prompt,
                str(repair_parent),
                repair_handoff.builder_generation,
            )
            if repair is None:
                return
            self._complete_attempt(
                node,
                context,
                attempt,
                record,
                basis,
                _pending_gate(),
                baseline,
                repair.execution,
                record.integration_head,
                record_result=False,
            )
            return

        try:
            execution = (
                None if already_claimed else self.deps.recover_node(attempt)
            )
        except (KeyboardInterrupt, SigintInterrupt):
            raise
        except BaseException as exc:
            raise DurableOutputIdentityError(
                f"{stranded.node_id}#{stranded.attempt_no}: "
                f"late-envelope recovery failed: {_exception_reason(exc)}"
            ) from exc
        if execution is None or not execution.envelope_parsed:
            payload = store.accepted_result_payload(
                self.run_id, node.node_id, stranded.attempt_no
            )
            if payload is None:
                raise DurableOutputIdentityError(
                    f"{stranded.node_id}#{stranded.attempt_no}: "
                    "late envelope is not usable"
                )
            execution = NodeExecution(
                envelope_parsed=True, exit_code=0, envelope_payload=payload
            )
        if not already_claimed:
            # A late envelope proves that the retained builder declared a
            # result; it does not prove that the builder has stopped writing.
            # Close that actor boundary before taking any inventory or
            # reconciling a sealed descendant. Otherwise recovery can commit a
            # partial tree, observe the builder's final writes as post-commit
            # divergence, and incorrectly release a fresh attempt.
            self._quiesce(record, "late-envelope-before-inventory")
            context.settled = True
        pre_verdict = vf.GateVerdict(
            green=False,
            unparseable=False,
            counts=None,
            reason="recovered generation crossed pre-gate before baseline",
        )
        basis = None
        if record.repair_of_attempt is not None:
            basis = rp.RepairBasis(
                base_sha=record.base_sha,
                integration_head=record.integration_head,
                repair_of_attempt=record.repair_of_attempt,
                chain_length=record.repair_chain_length,
            )
        elif isinstance(repair_parent, str):
            candidate = store.candidate(self.run_id, node.node_id, repair_parent)
            if candidate is None:
                raise DurableOutputIdentityError(
                    f"{node.node_id}#{record.attempt_no}: repair parent "
                    f"{repair_parent} is not a published candidate"
                )
            basis = rp.RepairBasis(
                base_sha=repair_parent,
                integration_head=record.integration_head,
                repair_of_attempt=record.attempt_no,
                chain_length=candidate.candidate_seq + 1,
            )
        self._complete_attempt(
            node,
            context,
            attempt,
            record,
            basis,
            pre_verdict,
            baseline,
            execution,
            record.integration_head,
            sealed_output_sha=sealed_output_sha,
        )

    def _complete_attempt(
        self,
        node: st.PlanNode,
        context: _AttemptContext,
        attempt: wt.AttemptWorktree,
        record: st.AttemptRecord,
        basis: Optional[rp.RepairBasis],
        pre_verdict: vf.GateVerdict,
        baseline: wt.Inventory,
        execution: NodeExecution,
        head: str,
        *,
        record_result: bool = True,
        sealed_output_sha: Optional[str] = None,
    ) -> None:
        store = self.deps.store
        if record_result:
            self._record_result(node, record, execution)
        self._require_running(record)
        if execution.launched_pid is not None:
            # A runner that reports its pid only on return still arms the
            # signals, just late — recorded so the row is complete either way.
            with self._lock:
                self._require_running(record)
                store.mark_launched(
                    self.run_id, node.node_id, record.attempt_no, execution.launched_pid
                )

        after = wt.inventory(attempt.path)
        self._require_running(record)
        measured = wt.delta(baseline, after)
        permission = wt.permission_check(attempt, measured, node.outputs)
        ignored = wt.existing_ignored_outputs(
            attempt.path, node.outputs, after, attempt.ignored_at_base
        )
        if ignored:
            # The file is on disk and matches a declared output, but git
            # will not carry it. An empty commit here is a silent success.
            verdict = vf.VerificationVerdict(
                verified=False,
                failed_clause=4,
                reason=(
                    "declared output exists on disk but is gitignored "
                    "and cannot reach the commit: " + ", ".join(ignored)
                ),
                block_reason=st.BlockReason.DECLARED_OUTPUT_UNCOMMITTABLE,
                offending_paths=ignored,
            )
            self._settle_context(context)
            self._settle_verdict(node, verdict, execution, record)
            return

        unreachable = self._unreferenced_produced_symbols(
            attempt, baseline, measured, after
        )
        if unreachable:
            # `min_cases` is a floor with no ceiling (#118). Evaluated here,
            # beside the ignored-output refusal and before the commit, because
            # it is the same kind of fact: a count taken over the measured
            # delta that no model was asked about and no later stage recovers.
            verdict = vf.adjudicate_reachability(unreachable, node.kind)
            self._settle_context(context)
            self._settle_verdict(node, verdict, execution, record)
            return

        if node.kind is st.NodeKind.CODE:
            verdict = vf.verify_code_node(
                exit_code=execution.exit_code,
                permission=permission,
                diff_empty=not measured.touched,
                expects_changes=node.expects_changes,
            )
        elif node.kind is st.NodeKind.TESTS:
            verdict = tc.verify_tests_node(
                envelope_parsed=execution.envelope_parsed,
                permission=permission,
                written=measured.touched,
                new_case_count=0,
            )
        else:
            # Clause 4 is evaluated here, at measurement, and the commit
            # follows it immediately (§8.4). Clause 3 is evaluated after the
            # commit, against the committed tree, so it is passed in below
            # only once the commit has been taken.
            verdict = vf.verify_agent_node(
                envelope_parsed=execution.envelope_parsed,
                pre_gate=pre_verdict,
                post_gate=_pending_gate(),
                permission=permission,
                repairing=basis is not None,
            )

        if not verdict.verified and verdict.failed_clause != 3:
            if self._continue_before_candidate_failure(
                node,
                context,
                attempt,
                record,
                basis,
                pre_verdict,
                baseline,
                verdict,
                execution,
                head,
            ):
                return
            self._settle_context(context)
            self._settle_verdict(node, verdict, execution, record)
            return

        if basis is not None and measured.is_empty:
            # #113: an empty repair mints no new sha. In a persistent review
            # lane this is another pre-candidate semantic failure, so spend
            # the durable same-session budget and ask the retained builder to
            # repair again. Falling through to the legacy attempt settlement
            # leaves the acknowledged handoff recoverable; the scheduler then
            # reclaims the same cancelled attempt forever without spending a
            # lane retry.
            empty_repair = vf.VerificationVerdict(
                verified=False,
                reason=rp.REPAIR_DIFF_EMPTY,
                retry_class=st.RetryClass.SEMANTIC,
            )
            if self._continue_before_candidate_failure(
                node,
                context,
                attempt,
                record,
                basis,
                pre_verdict,
                baseline,
                empty_repair,
                execution,
                head,
            ):
                return
            self._settle_context(context)
            self._settle_verdict(
                node,
                empty_repair,
                execution,
                record,
                extra_facts={rp.REPAIR_DIFF_EMPTY_KEY: True},
            )
            return

        if sealed_output_sha is None:
            with self._lock:
                self._require_running(record)
                # Builder-authored sealed descendant of this attempt: same
                # lineage check recover_sealed_descendant already uses.
                # commit_measured_delta with a stale attempt.base would raise
                # HeadMoved and spend environmental budget on recovered work.
                current_head = wt.resolve_commit(attempt.path, "HEAD")
                if current_head != attempt.base:
                    output_sha = wt.sealed_descendant_tip(attempt, attempt.base)
                    # Adopting a tip skips commit_measured_delta, and with it
                    # §8.4's staging assertion; check_post_commit then compares
                    # the working tree, never the sealed commit's tree. Assert
                    # the adopted tree against the measured after-state here or
                    # nothing does.
                    wt.assert_tip_matches_measured(
                        attempt, output_sha, measured, after
                    )
                else:
                    output_sha = wt.commit_measured_delta(
                        attempt,
                        measured,
                        after,
                        f"{node.node_id} attempt {record.attempt_no}",
                    )
                store.record_sealed_output(
                    self.run_id, node.node_id, record.attempt_no, output_sha
                )
        else:
            output_sha = sealed_output_sha
        self._require_running(record)
        expected = wt.expected_inventory(baseline, measured, after)
        committed = wt.check_post_commit(attempt, expected)
        self._require_running(record)
        if not committed.ok:
            # §8.3: at the post-commit evaluation a divergence from the
            # expected full inventory **convicts**. Nothing but the harness has
            # executed between the after-inventory and §8.4's index refresh, so
            # a divergence is a stray write inside a window in which nothing is
            # permitted to write — ENVIRONMENTAL by §8.4's own rule for that
            # window, a fact about the machine rather than a verdict about the
            # work. The verdict used to be computed here and dropped on the
            # floor, which is the same defect as the pre-merge evaluation never
            # running at all: §8.3 specifies two evaluations with two
            # consequences and production delivered neither.
            self._settle_context(context)
            self._settle_failure(
                node,
                rp.Classification(
                    retry_class=st.RetryClass.ENVIRONMENTAL,
                    reason=_check_result_reason(committed),
                ),
                record=record,
            )
            return

        if node.kind is st.NodeKind.AGENT:
            self._require_running(record)
            post = self.deps.run_gate(attempt, node, "post", self._cancelled.is_set)
            self._require_running(record)
            verdict = vf.verify_agent_node(
                envelope_parsed=execution.envelope_parsed,
                pre_gate=pre_verdict,
                post_gate=vf.adjudicate_gate(post, node.gate_min_cases),
                permission=permission,
                repairing=basis is not None,
            )
            if not verdict.verified:
                if self._continue_after_preacceptance_failure(
                    node, context, attempt, record, output_sha, verdict, execution, head
                ):
                    return
                self._settle_context(context)
                self._settle_verdict(node, verdict, execution, record)
                return
            if self.test_strength_contract is st.TestStrengthContract.STRENGTH_V1:
                self._require_running(record)
                paired = self._bind_test_pairing(node, attempt, output_sha)
                self._require_running(record)
                if not paired.verified:
                    if self._continue_after_preacceptance_failure(
                        node, context, attempt, record, output_sha, paired,
                        execution, head
                    ):
                        return
                    self._settle_context(context)
                    self._settle_verdict(node, paired, execution, record)
                    return
        elif node.kind is st.NodeKind.TESTS:
            self._require_running(record)
            parent_red = self._prove_tests_red_at_parent(node, attempt, measured)
            self._require_running(record)
            if not parent_red.verified:
                self._settle_context(context)
                self._settle_verdict(node, parent_red, execution, record)
                return
            if self.test_strength_contract is st.TestStrengthContract.STRENGTH_V1:
                self._require_running(record)
                strength = self._prove_test_strength(
                    node, attempt, measured, output_sha
                )
                self._require_running(record)
                if not strength.verified:
                    self._settle_context(context)
                    self._settle_verdict(node, strength, execution, record)
                    return

        # The committed candidate is now immutable, but not mergeable.  The
        # builder stays quiescent in the same worktree while its derived review
        # is authoritative; settlement happens only after PASS or a terminal
        # block/cancel path.
        residue = wt.check_pre_merge(attempt, expected)
        if residue.cleanliness is not None and not residue.cleanliness.clean:
            self._report_hygiene(
                node.node_id,
                tuple(
                    "{0} {1}".format(d.kind, d.path)
                    for d in residue.cleanliness.divergences
                ),
            )
        if residue.unprovisioned_worktrees:
            self._report_hygiene(
                node.node_id,
                tuple(
                    "unprovisioned-worktree {0}".format(entry)
                    for entry in residue.unprovisioned_worktrees
                ),
            )

        if node.kind is st.NodeKind.AGENT:
            falsified = self._falsify_outputs(
                node,
                attempt,
                basis.integration_head if basis is not None else attempt.base,
            )
            self._require_running(record)
            if not falsified.verified:
                if self._continue_after_preacceptance_failure(
                    node,
                    context,
                    attempt,
                    record,
                    output_sha,
                    falsified,
                    execution,
                    head,
                ):
                    return
                self._settle_context(context)
                self._settle_verdict(node, falsified, execution, record)
                return

        if not self._publish_and_review_candidate(
            node, context, attempt, record, baseline, execution, head, output_sha
        ):
            return

        with self._lock:
            self._require_running(record)
            self._output_shas[node.node_id] = output_sha
            store.mark_verified(self.run_id, node.node_id, output_sha)

    def _falsify_outputs(
        self, node: st.PlanNode, attempt: wt.AttemptWorktree, falsify_base: str
    ) -> "vf.VerificationVerdict":
        """§7.4's post-work half: take the subject back out and re-ask the gate.

        Here, and not earlier, because it needs a green post-node gate to be
        meaningful — the question is whether *that* green survives the removal
        of the code it was supposed to be about. Before `_settle_context` and
        before `mark_verified`, for the reasons the review stage below gives:
        the gate is a harness process like the two that ran already, and
        VERIFIED is what `_merge_frontier` reads.

        The cost is one extra gate run per agent attempt, which roughly
        matches the cost §7.4 already states for running the gate twice.

        The check has no subject when every path the attempt wrote is selected
        by the node's own gate — a node that produced nothing but the files its
        gate counts. That is a count (`len(unnamed) == 0`), the same hollow
        class `TESTS_HOLLOW_AT_PARENT` convicts for tests nodes. Refused as
        SEMANTIC, never reported as `verified=True` (#123). Tests nodes do
        not reach this method.
        """
        argv = tuple(node.gate_command)[1:]
        written = wt.paths_written_since(attempt, falsify_base)
        unnamed = vf.outputs_unnamed_by_gate(written, argv)
        if not unnamed:
            return vf.adjudicate_output_falsification(
                vf.GateVerdict(green=False, unparseable=False, counts=None), ()
            )
        reverted = wt.revert_paths_to(attempt, falsify_base, unnamed)
        try:
            result = self.deps.run_gate(
                attempt, node, "falsify", self._cancelled.is_set
            )
        finally:
            wt.restore_paths_from_head(attempt, reverted)
        return vf.adjudicate_output_falsification(
            vf.adjudicate_gate(result, node.gate_min_cases), reverted
        )

    def _prove_tests_red_at_parent(
        self,
        node: st.PlanNode,
        attempt: wt.AttemptWorktree,
        measured: "wt.InventoryDelta",
    ) -> "vf.VerificationVerdict":
        """Tests-node evidence: new cases, each red at this attempt's base.

        The worktree *is* the parent commit plus the tests this node wrote,
        so running the new nodeids here is the parent-red check. A collection
        error or import crash is not a satisfying red. Newly created test
        files have no parent nodeids, which is the ordinary case this chain
        is for.

        The selector lives on the written test files rather than on the gate
        command, but the *runner* does not: collection and execution are
        dispatched on `node.gate_command[0]`, exactly as the strength contract
        below does. Measuring with pytest whatever the gate declared is the
        silent-zero `tc.RunnerUnsupported` exists to refuse, and reaching it
        through this path instead reported `TESTS_NO_NEW_CASES` about a vitest
        node on every attempt — a refusal no edit to the tests could satisfy,
        so the node never merged and its derived reviewer never dispatched.
        """
        runner_name = node.gate_command[0] if node.gate_command else ""
        try:
            runner = tc.case_runner(runner_name)
        except tc.RunnerUnsupported as exc:
            return vf.VerificationVerdict(
                verified=False,
                failed_clause=3,
                reason=str(exc),
                retry_class=st.RetryClass.ENVIRONMENTAL,
                refusal_code=tc.StrengthRefusal.RUNNER_UNSUPPORTED.value,
                remedy=tc.StrengthRefusal.RUNNER_UNSUPPORTED.remedy,
            )
        test_paths = tuple(p for p in measured.touched if tc.is_test_path(p))
        current = runner.collect(attempt.path, test_paths)
        try:
            parent = tc.collect_parent_nodeids(
                attempt.path, attempt.base, test_paths, runner=runner
            )
        except tc.TestsGitReadFailed as exc:
            return vf.VerificationVerdict(
                verified=False,
                failed_clause=3,
                reason="{0}: {1}".format(tc.TestsRefusal.COLLECTION_FAILED.value, exc),
                retry_class=st.RetryClass.ENVIRONMENTAL,
                refusal_code=tc.TestsRefusal.COLLECTION_FAILED.value,
                remedy=tc.TestsRefusal.COLLECTION_FAILED.remedy,
            )
        new = tc.new_nodeids(parent, current)
        parent_run = tc.run_cases_for(runner, attempt.path, new)
        return tc.adjudicate_parent_red(parent_run, len(new))

    def _tests_prerequisites(self, node: st.PlanNode) -> Tuple[st.PlanNode, ...]:
        """The tests nodes this implementation node is gated by.

        Read from the **authored** graph, not the projection: the projection
        rewrote each dependency to the derived review id, and the thing this
        needs is the tests node itself and its declared contract.
        """
        authored = self.authored_nodes.get(node.node_id)
        if authored is None:
            return ()
        # `authored.needs`, never `node.needs`: the node the worker holds is
        # the *projected* one, whose dependency on a tests node was rewritten
        # to that node's derived review id. Reading the projected edge here
        # finds `tests::review` in `authored_nodes`, finds nothing, and
        # answers "this implementation is gated by no tests" for every pair in
        # every run -- which is the field-reads-as-its-default shape §19 M26
        # records, one layer along.
        return tuple(
            self.authored_nodes[dependency]
            for dependency in authored.needs
            if self._is_tests_node(dependency)
        )

    def _bind_test_pairing(
        self,
        node: st.PlanNode,
        attempt: wt.AttemptWorktree,
        output_sha: str,
    ) -> "vf.VerificationVerdict":
        """Bind this implementation candidate to the exact test bytes it passed.

        Three facts, each proven against immutable objects rather than
        inferred from the tree that happens to be present:

        1. the tests node has an accepted candidate — strong measured
           evidence and a passed independent review, bound to one sha;
        2. every file that candidate declared is **byte-identical** in this
           implementation candidate's own commit. An implementation that
           edited, weakened, or deleted one of the reviewed test files is not
           gated by the tests that were reviewed, and letting it inherit that
           acceptance is obligation 9's substitution;
        3. the accepted candidate's coverage obligations are green here.
           That is obligation 5 stated the only way it can be measured: the
           same selectors, the same cases, now passing against the candidate
           they were written to gate.

        The pairing row is written last and is the merge check's authority.
        Without it a merge would have to re-derive all three from mutable
        state, and the answer would be yes for any test tree present.
        """
        for tests_node in self._tests_prerequisites(node):
            accepted = self.deps.store.accepted_test_candidate(
                self.run_id, tests_node.node_id
            )
            if accepted is None:
                return self._pairing_refused(
                    tc.PairingRefusal.NO_ACCEPTED_CANDIDATE,
                    "{0} has no accepted test candidate, so {1} has nothing "
                    "to be gated by".format(tests_node.node_id, node.node_id),
                    retry_class=st.RetryClass.ENVIRONMENTAL,
                )
            declared = tuple(tests_node.outputs)
            try:
                differing = tc.compare_test_bytes(
                    Path(attempt.repo), accepted.candidate_sha, output_sha,
                    declared)
            except tc.TestsGitReadFailed as exc:
                return self._pairing_refused(
                    tc.PairingRefusal.UNREADABLE, str(exc),
                    retry_class=st.RetryClass.ENVIRONMENTAL)
            if differing:
                return self._pairing_refused(
                    tc.PairingRefusal.BYTES_SUBSTITUTED,
                    "{0} does not carry the accepted test candidate {1} "
                    "verbatim; these files differ: {2}".format(
                        node.node_id, accepted.candidate_sha[:12],
                        ", ".join(differing)))
            strength = tests_node.test_strength
            if strength is None:
                continue
            try:
                runner = tc.case_runner(accepted.runner)
            except tc.RunnerUnsupported as exc:
                return self._pairing_refused(
                    tc.PairingRefusal.UNREADABLE, str(exc),
                    retry_class=st.RetryClass.ENVIRONMENTAL)
            collected = runner.collect(attempt.path, declared)
            # Every counted case must be one the *accepted* test bytes define.
            # The candidate's own tree is not consulted for this: the forgery
            # this refuses runs at collection time and leaves the test file
            # byte-identical, so `compare_test_bytes` above passes truthfully
            # and the counting rule counts five cases that really did run --
            # three of them written by the code being gated (§16.3 item 8,
            # measured on `lane-routing-chemical` a3). Reading the accepted
            # blob from git and comparing names here is the one place the
            # process that would have to lie is not the process running the
            # tests.
            for declared_path in declared:
                accepted_bytes = tc._blob_at(
                    Path(attempt.repo), accepted.candidate_sha, declared_path)
                if accepted_bytes is None:
                    continue
                try:
                    # The runner is the one already resolved from the
                    # accepted candidate above, so the names are read in the
                    # language the tests are written in rather than in Python.
                    strays = gate_capture.unexpected_cases(
                        accepted_bytes.decode("utf-8", "replace"), collected,
                        runner)
                except gate_capture.GateCaptureRefusal as exc:
                    return self._pairing_refused(
                        tc.PairingRefusal.UNREADABLE, str(exc),
                        retry_class=st.RetryClass.ENVIRONMENTAL)
                if strays:
                    return self._pairing_refused(
                        tc.PairingRefusal.GATE_NOT_GREEN,
                        "{0}: {1} counted case(s) the accepted test candidate "
                        "{2} does not define: {3}".format(
                            gate_capture.CASE_NOT_IN_ACCEPTED_TESTS,
                            len(strays), accepted.candidate_sha[:12],
                            ", ".join(strays)),
                        remedy=(
                            "Remove whatever defines or generates the stray "
                            "cases named above — conftest hooks, "
                            "parametrization, or collection-time code in "
                            "this diff. Only cases the accepted test bytes "
                            "define may count toward the gate; the count "
                            "must be reached by making those cases pass, "
                            "never by adding cases of your own."))
                # The root cause, refused before the builder is blamed for it.
                # A build lane carries the accepted test bytes verbatim, so the
                # collectable case count is fixed before it starts; a gate
                # demanding more than the reviewed tests define is unsatisfiable
                # by construction and every honest attempt fails it forever.
                # That is the condition that produced the forgery above, not a
                # separate problem: `lane-routing-chemical` was told to reach 5
                # against a test candidate defining 2.
                shortfall = gate_capture.unsatisfiable_min_cases(
                    accepted_bytes.decode("utf-8", "replace"),
                    node.gate_min_cases, runner)
                if shortfall:
                    return self._pairing_refused(
                        tc.PairingRefusal.GATE_NOT_GREEN,
                        "{0}: {1} demands min_cases={2} but the accepted test "
                        "candidate {3} defines {4} case(s) and this node may "
                        "not change them -- no honest attempt can pass".format(
                            gate_capture.MIN_CASES_UNSATISFIABLE,
                            node.node_id, node.gate_min_cases,
                            accepted.candidate_sha[:12],
                            node.gate_min_cases - shortfall),
                        retry_class=st.RetryClass.ENVIRONMENTAL,
                        remedy=(
                            "No edit of this diff can satisfy the gate: the "
                            "accepted tests define fewer cases than the "
                            "gate's min_cases and this node may not change "
                            "them. An operator must re-ship the plan with "
                            "min_cases at or below the defined case count, "
                            "or re-run the tests lane so a candidate "
                            "defining enough cases is accepted."))
            positive = runner.run(attempt.path, collected)
            coverage = tc.measure_coverage(
                strength.coverage, positive, tc.PASSED)
            if not coverage.covered:
                return self._pairing_refused(
                    tc.PairingRefusal.GATE_NOT_GREEN,
                    "{0}: {1}".format(coverage.refusal, coverage.reason))
            self.deps.store.record_test_pairing(
                self.run_id,
                node.node_id,
                tests_node.node_id,
                accepted_test_sha=accepted.candidate_sha,
                implementation_sha=output_sha,
                verifier_command=" ".join(positive.command),
                selector=" ".join(declared),
                executed_cases=positive.passed,
                coverage=coverage.as_mapping(),
            )
        return vf.VerificationVerdict(verified=True)

    @staticmethod
    def _pairing_refused(
        code: "tc.PairingRefusal",
        detail: str,
        retry_class: st.RetryClass = st.RetryClass.SEMANTIC,
        remedy: Optional[str] = None,
    ) -> "vf.VerificationVerdict":
        # `remedy` overrides the member's default only where one code covers
        # several measured sub-causes (GATE_NOT_GREEN); the override is still
        # deterministic text declared at the raising site, never inferred
        # from the verdict afterwards.
        return vf.VerificationVerdict(
            verified=False,
            failed_clause=3,
            reason="{0}: {1}".format(code.value, detail),
            retry_class=retry_class,
            refusal_code=code.value,
            remedy=code.remedy if remedy is None else remedy,
        )

    def _prove_test_strength(
        self,
        node: st.PlanNode,
        attempt: wt.AttemptWorktree,
        measured: "wt.InventoryDelta",
        output_sha: str,
    ) -> "vf.VerificationVerdict":
        """Measure the candidate against its declared test-strength contract.

        Runs before the candidate is published for review, so a candidate that
        cannot discriminate never occupies a reviewer's turn, and the reviewer
        that does open is reading a candidate whose mechanical facts are
        already established. The evidence row is written **whether or not the
        candidate passes**: a rejection that leaves no record is a rejection
        nobody can audit, and the refusal reason is exactly what the retry
        prompt carries back to the retained tester.

        Two measurements, one run each:

        1. every coverage obligation selects at least `min_cases` cases that
           executed and reached a verdict. `EXECUTED`, not `PASSED`, because
           the implementation these cases gate does not exist yet -- they are
           all red here, and demanding green would be demanding the pair be
           built backwards;
        2. the declared negative control turns the declared cases red for the
           declared reason.

        **Coverage is measured over every case in the node's own files, not
        only the new ones.** A tests node that adds a boundary case to an
        existing file discharges its obligations with the whole file's cases,
        which is the honest reading of "is this requirement covered". What
        stops that from accepting a candidate that added nothing is the clause
        before this one: `_prove_tests_red_at_parent` already requires at
        least one genuinely new case and requires every new case to be red.

        Nothing here reads the envelope, the agent's report, or a pass count.
        """
        strength = node.test_strength
        runner_name = (node.gate_command[0] if node.gate_command else "")
        selector = node.gate_selector or ""
        test_paths = tuple(p for p in measured.touched if tc.is_test_path(p))
        if strength is None:
            uncontracted = tc.GateStrengthEvidence(
                tests_node_id=node.node_id, candidate_sha=output_sha,
                runner=runner_name, selector=selector,
                contract_declared=False,
                gate_min_cases=node.gate_min_cases)
            self._record_strength_evidence(
                node, output_sha, runner_name, selector, uncontracted)
            return tc.verify_test_strength(uncontracted)
        try:
            runner = tc.case_runner(runner_name)
        except tc.RunnerUnsupported as exc:
            return vf.VerificationVerdict(
                verified=False, failed_clause=3, reason=str(exc),
                retry_class=st.RetryClass.ENVIRONMENTAL,
                refusal_code=tc.StrengthRefusal.RUNNER_UNSUPPORTED.value,
                remedy=tc.StrengthRefusal.RUNNER_UNSUPPORTED.remedy)
        try:
            current = runner.collect(attempt.path, test_paths)
            parent = tc.collect_parent_nodeids(
                attempt.path, attempt.base, test_paths, runner=runner)
        except tc.TestsGitReadFailed as exc:
            return vf.VerificationVerdict(
                verified=False, failed_clause=3,
                reason="{0}: {1}".format(
                    tc.TestsRefusal.COLLECTION_FAILED.value, exc),
                retry_class=st.RetryClass.ENVIRONMENTAL,
                refusal_code=tc.TestsRefusal.COLLECTION_FAILED.value,
                remedy=tc.TestsRefusal.COLLECTION_FAILED.remedy)
        new = tc.new_nodeids(parent, current)
        coverage_run = runner.run(attempt.path, current)
        coverage = tc.measure_coverage(
            strength.coverage, coverage_run, tc.EXECUTED)
        falsifiability = tc.FalsifiabilityResult(
            strategy=str(strength.falsifiability.strategy))
        if coverage.covered:
            base = (self.deps.plan_base_commit or "").strip()
            if (strength.falsifiability.strategy == "controlled_mutation"
                    and not base):
                falsifiability = tc.FalsifiabilityResult(
                    strategy=str(strength.falsifiability.strategy),
                    refusal=tc.StrengthRefusal.CONTROL_UNEXECUTABLE.value,
                    reason=("this scheduler was given no plan base commit, so "
                            "a revert_paths control has no target to revert "
                            "to; it is refused rather than run against HEAD"))
            else:
                try:
                    falsifiability = tc.execute_negative_control(
                        falsifiability=strength.falsifiability,
                        runner=runner,
                        repo=Path(attempt.repo),
                        tree=attempt.path,
                        candidate_sha=output_sha,
                        base_commit=base or attempt.base,
                        nodeids=current,
                        scratch_root=Path(self.deps.scratch_root),
                        # The coverage run and a `baseline_absent` control are
                        # the same command over the same immutable tree. Handed
                        # over rather than re-run, so the reviewer, the ledger
                        # and the verdict all rest on one execution.
                        already_executed=coverage_run,
                    )
                except (tc.NegativeControlUnexecutable,
                        tc.TestsGitReadFailed) as exc:
                    falsifiability = tc.FalsifiabilityResult(
                        strategy=str(strength.falsifiability.strategy),
                        refusal=tc.StrengthRefusal.CONTROL_UNEXECUTABLE.value,
                        reason=str(exc))
        evidence = tc.GateStrengthEvidence(
            tests_node_id=node.node_id,
            candidate_sha=output_sha,
            runner=runner_name,
            selector=selector,
            contract_declared=True,
            executed_nodeids=tuple(current),
            new_nodeids=tuple(new),
            coverage=coverage,
            falsifiability=falsifiability,
            gate_min_cases=node.gate_min_cases,
        )
        self._record_strength_evidence(
            node, output_sha, runner_name, selector, evidence)
        return tc.verify_test_strength(evidence)

    def _record_strength_evidence(
        self,
        node: st.PlanNode,
        output_sha: str,
        runner_name: str,
        selector: str,
        evidence: "tc.GateStrengthEvidence",
    ) -> None:
        """Persist the measurement. Exactly once per immutable candidate."""
        self.deps.store.record_test_gate_evidence(
            self.run_id,
            node.node_id,
            output_sha,
            runner=runner_name,
            selector=selector,
            strong=evidence.strong,
            refusal=evidence.refusal,
            evidence=evidence.as_mapping(),
        )

    def _active_actor_generation(
        self, node_id: str, actor_role: str, record: st.AttemptRecord
    ) -> int:
        """Read the latest durable actor generation that fences a callback."""
        session = self.deps.store.current_actor_session(
            self.run_id, node_id, actor_role
        )
        if session is not None:
            return session.generation
        sessions = self.deps.store.actor_sessions(
            self.run_id, node_id, actor_role=actor_role, limit=10_000
        )
        return max(
            record.attempt_no,
            max((item.generation for item in sessions), default=0),
        )

    def _require_actor_generation(
        self, node_id: str, actor_role: str, generation: int, record: st.AttemptRecord
    ) -> None:
        self._require_running(record)
        active = self.deps.store.current_actor_session(self.run_id, node_id, actor_role)
        if active is not None and active.generation != generation:
            raise AttemptOwnershipLost(
                f"{node_id}: {actor_role} generation {generation} was superseded"
            )

    def _set_lane_phase(
        self,
        node_id: str,
        phase: st.LanePhase,
        record: st.AttemptRecord,
        *,
        allow_watchdog_fence: bool = False,
    ) -> None:
        """CAS a lane phase while this builder generation owns RUNNING."""
        if not allow_watchdog_fence:
            self._require_running(record)
        current = self.deps.store.get_node(self.run_id, node_id).lane_phase
        if current is phase:
            return
        if current in st.LANE_PHASE_TERMINAL or not self.deps.store.set_lane_phase(
            self.run_id, node_id, phase, expected=current
        ):
            raise AttemptOwnershipLost(
                f"{node_id}: lane phase changed before {phase.value}"
            )

    def _review_evidence(
        self, review: Any
    ) -> Tuple[str, str, Tuple[Mapping[str, Any], ...]]:
        """Extract typed immutable evidence for the candidate review ledger."""
        digest = getattr(review, "subject_digest", None)
        if not isinstance(digest, str) or not digest:
            raise lc.LifecycleError("review outcome has no typed subject digest")
        if self.deps.receipt_path_for is None:
            raise lc.LifecycleError(
                "persistent candidate review requires receipt_path_for"
            )
        receipt_path = self.deps.receipt_path_for(digest)
        if not isinstance(receipt_path, str) or not receipt_path:
            raise lc.LifecycleError(
                "receipt_path_for did not return the review receipt path"
            )
        findings: List[Mapping[str, Any]] = []
        for cell in (
            tuple(getattr(review, "findings", ()))
            + tuple(getattr(review, "advisories", ()))
            + tuple(getattr(review, "unreachable", ()))
        ):
            if isinstance(cell, Mapping):
                findings.append(dict(cell))
            else:
                findings.append(
                    {
                        "check_id": str(getattr(cell, "check_id", "")),
                        "object_id": str(getattr(cell, "object_id", "")),
                        "message": str(getattr(cell, "message", "")),
                        "grade": getattr(cell, "grade", None),
                    }
                )
        return digest, receipt_path, tuple(findings)

    def _repair_prompt(self, rejected_candidate_sha: str, review: Any) -> str:
        findings_text = getattr(review, "findings_text", None)
        rendered = findings_text() if callable(findings_text) else ""
        if not rendered:
            lines = []
            for finding in getattr(review, "findings", ()):
                if isinstance(finding, Mapping):
                    check_id = str(finding.get("check_id", "")).strip()
                    object_id = str(finding.get("object_id", "")).strip()
                    message = str(finding.get("message", "")).strip()
                    rationale = str(
                        finding.get("grade_rationale", "")
                        or finding.get("rationale", "")
                    ).strip()
                else:
                    check_id = str(getattr(finding, "check_id", "")).strip()
                    object_id = str(getattr(finding, "object_id", "")).strip()
                    message = str(getattr(finding, "message", "")).strip()
                    rationale = str(
                        getattr(finding, "grade_rationale", "")
                        or getattr(finding, "rationale", "")
                    ).strip()
                label = " / ".join(part for part in (check_id, object_id) if part)
                line = f"- {label}: {message}" if label else f"- {message}"
                if rationale:
                    line += f" Rationale: {rationale}"
                lines.append(line)
            rendered = "\n".join(lines)
        return (
            f"Repair rejected candidate {rejected_candidate_sha} in the existing "
            "worktree. Address every finding and emit one distinct descendant "
            f"candidate.\n\n{rendered}"
        )

    def _continue_repair_handoff(
        self,
        node: st.PlanNode,
        context: _AttemptContext,
        attempt: wt.AttemptWorktree,
        record: st.AttemptRecord,
        prompt: str,
        rejected_candidate_sha: str,
        builder_generation: int,
    ) -> Optional[RepairExecution]:
        """Deliver and durably acknowledge one handoff on the owning builder."""
        if self.deps.continue_node is None:
            self.deps.store.fail_handoff(
                self.run_id,
                node.node_id,
                rejected_candidate_sha,
                builder_generation=builder_generation,
                reason="persistent repair callback is unavailable",
            )
            self._close_persistent_reviewer(node.node_id, record)
            self._settle_context(context)
            self._set_lane_phase(node.node_id, st.LanePhase.BLOCKED, record)
            self.deps.store.mark_blocked(
                self.run_id,
                node.node_id,
                st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
                detail={
                    "candidate_sha": rejected_candidate_sha,
                    "reason": "persistent repair callback is unavailable",
                },
            )
            return None

        self._set_lane_phase(node.node_id, st.LanePhase.REPAIRING, record)
        while True:
            try:
                repair = self.deps.continue_node(
                    attempt,
                    node,
                    record,
                    prompt,
                    rejected_candidate_sha,
                    builder_generation,
                    self._cancelled.is_set,
                )
                break
            except UnserviceableHandoff as exc:
                # The precise site: this frame holds the rejected sha and the
                # generation, so the handoff row is failed by name instead of
                # being left PENDING forever behind a blocked node.
                self._close_persistent_reviewer(node.node_id, record)
                self._settle_context(context)
                self._block_unserviceable_handoff(
                    node,
                    record,
                    exc,
                    candidate_sha=rejected_candidate_sha,
                    builder_generation=builder_generation,
                )
                return None
            except (AttemptCancelled, AttemptOwnershipLost, QuiescenceFailure):
                raise
            except Exception as exc:
                retry_class = (
                    st.LaneRetryClass.LAUNCHER_TRANSIENT
                    if isinstance(exc, LaunchFailed)
                    else st.LaneRetryClass.ENVIRONMENTAL
                )
                detail = {
                    "candidate_sha": rejected_candidate_sha,
                    "reason": _exception_reason(exc),
                }
                allowed = self._lane_retry(
                    node,
                    retry_class,
                    candidate_sha=rejected_candidate_sha,
                    detail=detail,
                    launcher_failure=(
                        exc.classified_failure
                        if isinstance(exc, LaunchFailed)
                        else None
                    ),
                )
                if allowed:
                    builder_generation = self._active_actor_generation(
                        node.node_id, "builder", record
                    )
                    candidate = self.deps.store.candidate(
                        self.run_id, node.node_id, rejected_candidate_sha
                    )
                    guidance = self._refresh_lane_guidance(node.node_id, record)
                    prompt = rp.render_guidance(
                        node,
                        guidance,
                        repair=rp.RepairBasis(
                            base_sha=rejected_candidate_sha,
                            integration_head=record.integration_head,
                            repair_of_attempt=record.attempt_no,
                            chain_length=(
                                candidate.candidate_seq if candidate is not None else 1
                            ),
                        ),
                    )
                    continue
                self.deps.store.fail_handoff(
                    self.run_id,
                    node.node_id,
                    rejected_candidate_sha,
                    builder_generation=builder_generation,
                    reason=_exception_reason(exc),
                )
                self._close_persistent_reviewer(node.node_id, record)
                self._settle_context(context)
                self._set_lane_phase(node.node_id, st.LanePhase.BLOCKED, record)
                block_reason = (
                    _budget_reason(
                        st.RetryClass.LAUNCHER_TRANSIENT,
                        exc.classified_failure,
                    )
                    if isinstance(exc, LaunchFailed)
                    else st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED
                )
                self.deps.store.mark_blocked(
                    self.run_id,
                    node.node_id,
                    block_reason,
                    detail=detail,
                )
                return None

        replacement_generation = repair.builder_generation
        self._require_actor_generation(
            node.node_id, "builder", replacement_generation, record
        )
        submitted = self.deps.store.mark_handoff_submitted(
            self.run_id,
            node.node_id,
            rejected_candidate_sha,
            builder_generation=replacement_generation,
        )
        if not submitted.submitted:
            raise AttemptOwnershipLost(
                f"{node.node_id}: builder generation lost repair handoff"
            )
        self._quiesce(record, "repair-idle")
        if repair.acknowledged_rejected_sha != rejected_candidate_sha:
            self.deps.store.fail_handoff(
                self.run_id,
                node.node_id,
                rejected_candidate_sha,
                builder_generation=replacement_generation,
                reason="repair acknowledgement does not bind the active handoff",
            )
            raise AttemptOwnershipLost(
                f"{node.node_id}: repair acknowledgement is stale or mismatched"
            )
        acknowledged = self.deps.store.acknowledge_handoff(
            self.run_id,
            node.node_id,
            rejected_candidate_sha,
            builder_generation=replacement_generation,
        )
        if not acknowledged.acknowledged:
            raise AttemptOwnershipLost(
                f"{node.node_id}: repair acknowledgement lost lifecycle ownership"
            )
        self._set_lane_phase(
            node.node_id, st.LanePhase.WAITING_FOR_NEW_CANDIDATE, record
        )
        return repair

    def _lane_retry(
        self,
        node: st.PlanNode,
        retry_class: st.LaneRetryClass,
        *,
        candidate_sha: Optional[str],
        detail: Mapping[str, Any],
        launcher_failure: Optional[rp.LauncherFailure] = None,
    ) -> bool:
        """Spend one durable same-session correction-loop budget unit."""
        previous = self.deps.store.lane_retry_spends(
            self.run_id, node.node_id, limit=10_000
        )
        spend = self.deps.store.spend_lane_retry(
            self.run_id,
            node.node_id,
            retry_class,
            cycle_seq=len(previous) + 1,
            candidate_sha=candidate_sha,
            detail=detail,
        )
        if not spend.created:
            return True
        lifecycle = self.deps.store.get_node(self.run_id, node.node_id)
        if retry_class is st.LaneRetryClass.REVIEW_REJECTION:
            ceiling = self.config.review_ceiling + lifecycle.granted_extra_attempts
        elif retry_class is st.LaneRetryClass.TEST_REVIEW_REJECTION:
            ceiling = (
                self.config.test_review_ceiling + lifecycle.granted_extra_attempts
            )
        elif retry_class is st.LaneRetryClass.SEMANTIC:
            ceiling = self.config.semantic_ceiling + lifecycle.granted_extra_attempts
        elif retry_class is st.LaneRetryClass.LAUNCHER_TRANSIENT:
            ceiling = rp.launcher_retry_budget(self.config, launcher_failure)
        else:
            ceiling = self.config.environmental_retries
        # Every refreshable class reads the **floored** view, and there is no
        # longer an exception to it.
        #
        # This used to be a conditional: environmental and launcher spends were
        # counted from `lane_retry_spend_floor`, and SEMANTIC, REVIEW_REJECTION
        # and TEST_REVIEW_REJECTION were counted from the beginning of the
        # lane's life. That was coherent while a resume refreshed only the two
        # infrastructure classes -- an unfloored read of a budget no boundary
        # ever moved is just a total. It stopped being coherent the moment
        # `_RESUME_REFRESHED_BLOCK_REASONS` grew the semantic ceiling and the
        # bounded review ceiling: `resume_run` raised the floor, this read
        # ignored it, and the lane re-blocked on its next spend against every
        # cycle the boundary had already forgiven.
        #
        # It shipped inert for exactly that reason, and the shape of the miss is
        # worth keeping: the *attempt* budget (`attempts_spent`, floored on
        # `node_lifecycle.retry_spend_floor`) did honour the boundary, so tests
        # written against that accounting passed while this one stayed broken.
        # Two budgets, one boundary, and only one of them was reading it.
        # Production receipt: floor raised to 12 at 06:43:15,
        # `blocked:SEMANTIC_BUDGET_EXHAUSTED` at 06:43:28.
        budget_spends = self.deps.store.current_lane_retry_spends(
            self.run_id, node.node_id, limit=10_000
        )
        spent = sum(item.retry_class is retry_class for item in budget_spends)
        if retry_class in (
            st.LaneRetryClass.SEMANTIC,
            st.LaneRetryClass.REVIEW_REJECTION,
            st.LaneRetryClass.TEST_REVIEW_REJECTION,
        ):
            return spent < ceiling
        # Launcher/environmental settings name retries after the first
        # failure; semantic/review ceilings name the total rejected cycles.
        return spent <= ceiling

    def _refresh_lane_guidance(
        self, build_node_id: str, record: st.AttemptRecord
    ) -> rp.GuidanceLedger:
        """Rebuild one retained lane's prompt constraints from durable rows."""
        legacy = rp.guidance_from_attempts(
            self.deps.store.attempts_for(self.run_id)
        ).get(record.guidance_key, rp.GuidanceLedger())
        review_node = self._review_for_build(build_node_id)
        reviews = (
            ()
            if review_node is None
            else self.deps.store.candidate_reviews(
                self.run_id, review_node.node_id, limit=10_000
            )
        )
        persistent = rp.guidance_from_lane_history(
            self.deps.store.lane_retry_spends(self.run_id, build_node_id, limit=10_000),
            reviews,
        )
        combined = rp.GuidanceLedger(
            verification=legacy.verification + persistent.verification,
            review=legacy.review + persistent.review,
        )
        if combined.empty:
            self._guidance.pop(record.guidance_key, None)
        else:
            self._guidance[record.guidance_key] = combined
        return combined

    def _lane_semantic_budget_detail(
        self, detail: Mapping[str, Any], node_id: str
    ) -> Dict[str, Any]:
        """Describe the durable correction-loop spend that caused a block."""
        lifecycle = self.deps.store.get_node(self.run_id, node_id)
        spent = sum(
            item.retry_class is st.LaneRetryClass.SEMANTIC
            for item in self.deps.store.lane_retry_spends(
                self.run_id, node_id, limit=10_000
            )
        )
        granted = lifecycle.granted_extra_attempts
        required = spent + 2 - self.config.semantic_ceiling - granted
        return dict(
            detail,
            semantic_retry_spends_total=spent,
            semantic_ceiling=self.config.semantic_ceiling,
            granted_extra_attempts=granted,
            semantic_grant_required=max(1, required),
        )

    @staticmethod
    def _verdict_classification(
        node: st.PlanNode, verdict: "vf.VerificationVerdict", execution: NodeExecution
    ) -> rp.Classification:
        """Classify one deterministic verification failure identically everywhere."""
        signal = rp.FailureSignal(
            node_kind=node.kind,
            exit_code=execution.exit_code,
            gate=rp.GateOutcome(pre_gate_failed=True, post_gate_passed=False)
            if verdict.failed_clause == 3
            else None,
            report=rp.ReportOutcome(
                parsed=execution.envelope_parsed, failed=verdict.failed_clause == 3
            ),
            launcher_failure=execution.launcher_failure,
        )
        return (
            rp.Classification(retry_class=verdict.retry_class)
            if verdict.retry_class is not None
            else rp.classify(signal)
        )

    def _continue_before_candidate_failure(
        self,
        node: st.PlanNode,
        context: _AttemptContext,
        attempt: wt.AttemptWorktree,
        record: st.AttemptRecord,
        basis: Optional[rp.RepairBasis],
        pre_verdict: vf.GateVerdict,
        baseline: wt.Inventory,
        verdict: "vf.VerificationVerdict",
        execution: NodeExecution,
        head: str,
    ) -> bool:
        """Repair an uncommitted semantic failure in the retained worktree."""
        if (
            self._review_for_build(node.node_id) is None
            or self.deps.continue_node is None
            or verdict.block_reason is not None
        ):
            return False
        classification = self._verdict_classification(node, verdict, execution)
        if classification.retry_class is None or not st.mutates_prompt(
            classification.retry_class
        ):
            return False
        retry_class = st.LaneRetryClass(classification.retry_class.value)
        subject_sha = basis.base_sha if basis is not None else attempt.base
        detail = _failure_detail(classification, verdict)
        detail["candidate_sha"] = subject_sha
        # Read before this failure's spend is written, so the history is the
        # prior refusals (§7.5 refusal repetition).
        current = rp.verification_guidance(detail)
        repeats = self._lane_refusal_repeats(node.node_id, current)
        allowed = self._lane_retry(
            node, retry_class, candidate_sha=subject_sha, detail=detail
        )
        guidance = self._refresh_lane_guidance(node.node_id, record)
        if not allowed or repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
            self._close_persistent_reviewer(node.node_id, record)
            self._settle_context(context)
            self._set_lane_phase(node.node_id, st.LanePhase.BLOCKED, record)
            if repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
                # Identical typed refusal to the previous SEMANTIC failure of
                # this retained lane: re-prompting the same builder with a
                # ledger that deduplicates the identical entry is a prompt
                # that does not change, so the repair reproduces the refusal
                # until the budget is gone. Named ahead of the budget reason
                # because it is the truer cause (§7.5).
                reason = st.BlockReason.SEMANTIC_REFUSAL_REPEATED
                detail = rp.repeated_refusal_detail(detail, current, repeats)
            else:
                reason = _budget_reason(classification.retry_class)
                if retry_class is st.LaneRetryClass.SEMANTIC:
                    detail = self._lane_semantic_budget_detail(detail, node.node_id)
            self.deps.store.mark_blocked(
                self.run_id,
                node.node_id,
                reason,
                detail=detail,
                retry_class=classification.retry_class,
            )
            return True
        builder_generation = self._active_actor_generation(
            node.node_id, "builder", record
        )
        self._require_actor_generation(
            node.node_id, "builder", builder_generation, record
        )
        self._set_lane_phase(node.node_id, st.LanePhase.REPAIRING, record)
        repair_instruction = (
            "Repair the failed pre-candidate work in this existing worktree. "
            f"Failure: {verdict.reason}"
        )
        rendered = rp.render_guidance(node, guidance, repair=basis)
        repair_prompt = "\n\n".join(
            part for part in (repair_instruction, rendered) if part
        )
        repair = self.deps.continue_node(
            attempt,
            node,
            record,
            repair_prompt,
            subject_sha,
            builder_generation,
            self._cancelled.is_set,
        )
        self._quiesce(record, "repair-idle")
        if (
            repair.acknowledged_rejected_sha != subject_sha
            or repair.builder_generation != builder_generation
        ):
            raise AttemptOwnershipLost(
                f"{node.node_id}: pre-candidate repair acknowledgement mismatched"
            )
        self._set_lane_phase(
            node.node_id, st.LanePhase.WAITING_FOR_NEW_CANDIDATE, record
        )
        self._complete_attempt(
            node,
            context,
            attempt,
            record,
            basis,
            pre_verdict,
            baseline,
            repair.execution,
            head,
            record_result=False,
        )
        return True

    def _continue_after_preacceptance_failure(
        self,
        node: st.PlanNode,
        context: _AttemptContext,
        attempt: wt.AttemptWorktree,
        record: st.AttemptRecord,
        previous_sha: str,
        verdict: "vf.VerificationVerdict",
        execution: NodeExecution,
        head: str,
    ) -> bool:
        """Repair a committed-but-not-published failure in the same session."""
        if (
            self._review_for_build(node.node_id) is None
            or self.deps.continue_node is None
            or verdict.block_reason is not None
        ):
            return False
        classification = self._verdict_classification(node, verdict, execution)
        if classification.retry_class is None or not st.mutates_prompt(
            classification.retry_class
        ):
            return False
        retry_class = st.LaneRetryClass(classification.retry_class.value)
        detail = _failure_detail(classification, verdict)
        detail["candidate_sha"] = previous_sha
        # Read before this failure's spend is written, so the history is the
        # prior refusals (§7.5 refusal repetition).
        current = rp.verification_guidance(detail)
        repeats = self._lane_refusal_repeats(node.node_id, current)
        allowed = self._lane_retry(
            node, retry_class, candidate_sha=previous_sha, detail=detail
        )
        guidance = self._refresh_lane_guidance(node.node_id, record)
        if not allowed or repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
            self._close_persistent_reviewer(node.node_id, record)
            self._settle_context(context)
            self._set_lane_phase(node.node_id, st.LanePhase.BLOCKED, record)
            if repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
                # Identical typed refusal to the previous SEMANTIC failure of
                # this retained lane — see `_continue_after_precandidate_
                # failure`; the repair prompt would not change, so the loop
                # cannot converge (§7.5).
                reason = st.BlockReason.SEMANTIC_REFUSAL_REPEATED
                detail = rp.repeated_refusal_detail(detail, current, repeats)
            else:
                reason = _budget_reason(classification.retry_class)
                if retry_class is st.LaneRetryClass.SEMANTIC:
                    detail = self._lane_semantic_budget_detail(detail, node.node_id)
            self.deps.store.mark_blocked(
                self.run_id,
                node.node_id,
                reason,
                detail=detail,
                retry_class=classification.retry_class,
            )
            return True
        builder_generation = self._active_actor_generation(
            node.node_id, "builder", record
        )
        self._require_actor_generation(
            node.node_id, "builder", builder_generation, record
        )
        self._set_lane_phase(node.node_id, st.LanePhase.REPAIRING, record)
        repair_basis = rp.RepairBasis(
            base_sha=previous_sha,
            integration_head=head,
            repair_of_attempt=record.attempt_no,
            chain_length=1,
        )
        wt.prepare_descendant_candidate(attempt, previous_sha)
        repair_baseline = wt.take_baseline(attempt)
        repair_instruction = (
            "Repair the failed pre-acceptance candidate "
            f"{previous_sha}. Gate failure: {verdict.reason}"
        )
        rendered = rp.render_guidance(node, guidance, repair=repair_basis)
        repair_prompt = "\n\n".join(
            part for part in (repair_instruction, rendered) if part
        )
        repair = self.deps.continue_node(
            attempt,
            node,
            record,
            repair_prompt,
            previous_sha,
            builder_generation,
            self._cancelled.is_set,
        )
        self._quiesce(record, "repair-idle")
        if (
            repair.acknowledged_rejected_sha != previous_sha
            or repair.builder_generation != builder_generation
        ):
            raise AttemptOwnershipLost(
                f"{node.node_id}: pre-acceptance repair acknowledgement mismatched"
            )
        self._set_lane_phase(
            node.node_id, st.LanePhase.WAITING_FOR_NEW_CANDIDATE, record
        )
        self._complete_attempt(
            node,
            context,
            attempt,
            record,
            repair_basis,
            _pending_gate(),
            repair_baseline,
            repair.execution,
            head,
            record_result=False,
        )
        return True

    def _close_persistent_reviewer(
        self, node_id: str, record: st.AttemptRecord
    ) -> None:
        """Terminal paths close the reviewer; waiting paths never do."""
        if self.deps.close_review is None:
            return
        self._require_running(record)
        self.deps.close_review(node_id)

    def _publish_and_review_candidate(
        self,
        node: st.PlanNode,
        context: _AttemptContext,
        attempt: wt.AttemptWorktree,
        record: st.AttemptRecord,
        baseline: wt.Inventory,
        execution: NodeExecution,
        head: str,
        output_sha: str,
    ) -> bool:
        """Publish one candidate and drive its exactly-once review/repair loop."""
        review_node = self._review_for_build(node.node_id)
        if review_node is None or self.deps.review_attempt is None:
            return True
        candidates = self.deps.store.lane_candidates(
            self.run_id, node.node_id, limit=10_000
        )
        replay = next(
            (item for item in candidates if item.candidate_sha == output_sha), None
        )
        durable_handoff = (
            self.deps.store.repair_handoff(self.run_id, node.node_id, output_sha)
            if replay is not None
            else None
        )
        replayed_handoff = durable_handoff is not None
        repair_builder_generation = (
            durable_handoff.builder_generation
            if durable_handoff is not None
            else self._active_actor_generation(node.node_id, "builder", record)
        )
        # A replay reasserts immutable publication identity, not ownership of
        # the currently active builder.  New repair delivery below remains
        # fenced to that active builder; an existing handoff remains fenced to
        # its durable bound generation.
        publication_builder_generation = (
            replay.builder_generation
            if replay is not None and durable_handoff is None
            else repair_builder_generation
        )
        self._require_actor_generation(
            node.node_id, "builder", repair_builder_generation, record
        )
        parent = (
            replay.parent_candidate_sha
            if replay is not None
            else candidates[-1].candidate_sha
            if candidates
            else None
        )
        candidate = self.deps.store.publish_candidate(
            self.run_id,
            node.node_id,
            output_sha,
            parent_candidate_sha=parent,
            builder_generation=publication_builder_generation,
            repo_path=attempt.repo,
        ).candidate
        self._set_lane_phase(node.node_id, st.LanePhase.CANDIDATE_READY, record)
        reviewer_generation = self._active_actor_generation(
            node.node_id, "reviewer", record
        )
        self._set_lane_phase(node.node_id, st.LanePhase.REVIEWING, record)
        begun = self.deps.store.begin_review(
            self.run_id,
            review_node.node_id,
            candidate.candidate_sha,
            reviewer_generation=reviewer_generation,
        )
        durable_review = begun.review
        if durable_review.state in {
            st.CandidateReviewState.PUBLISHED,
            st.CandidateReviewState.DISPATCHED,
        }:
            # PUBLISHED means the candidate is durable but no reviewer prompt
            # has been proven submitted. DISPATCHED means the exact prompt was
            # submitted but no terminal report was consumed. Both need the
            # callback. The creation bit distinguishes a fresh publication
            # from recovery: replayed PUBLISHED checks the exact transcript
            # marker first; replayed DISPATCHED only polls.
            resume_existing_dispatch = not begun.created
            if durable_review.reviewer_generation != reviewer_generation:
                if reviewer_generation > durable_review.reviewer_generation:
                    recovered = self.deps.store.recover_review_dispatch(
                        self.run_id,
                        review_node.node_id,
                        candidate.candidate_sha,
                        expected_reviewer_generation=durable_review.reviewer_generation,
                        reviewer_generation=reviewer_generation,
                    )
                    durable_review = recovered.review
                    resume_existing_dispatch = True
                else:
                    # No active reviewer uses the build-attempt number only as
                    # an initial floor. Preserve the unfinished review's owner
                    # until the callback durably installs its replacement.
                    reviewer_generation = durable_review.reviewer_generation
            review = None
            while durable_review.state in {
                st.CandidateReviewState.PUBLISHED,
                st.CandidateReviewState.DISPATCHED,
            }:
                self._require_actor_generation(
                    node.node_id, "reviewer", reviewer_generation, record
                )
                try:
                    callback_review = self.deps.review_attempt(
                        attempt,
                        node,
                        record,
                        head,
                        candidate.candidate_sha,
                        resume_existing_dispatch,
                    )
                except cr.ReviewStalled as stall:
                    signal_name = _stall_signal(stall)
                    reason = (
                        signal_name or "the code reviewer stalled without reporting"
                    )
                    replacement_generation = self._active_actor_generation(
                        node.node_id, "reviewer", record
                    )
                    if replacement_generation > reviewer_generation:
                        recovered = self.deps.store.recover_review_dispatch(
                            self.run_id,
                            review_node.node_id,
                            candidate.candidate_sha,
                            expected_reviewer_generation=reviewer_generation,
                            reviewer_generation=replacement_generation,
                        )
                        durable_review = recovered.review
                        reviewer_generation = replacement_generation
                        if durable_review.terminal:
                            break
                    allowed = self._lane_retry(
                        node,
                        st.LaneRetryClass.ENVIRONMENTAL,
                        candidate_sha=candidate.candidate_sha,
                        detail={
                            "candidate_sha": candidate.candidate_sha,
                            "reason": reason,
                        },
                    )
                    if allowed:
                        resume_existing_dispatch = True
                        continue
                    self._close_persistent_reviewer(node.node_id, record)
                    self._settle_context(context)
                    self._set_lane_phase(node.node_id, st.LanePhase.BLOCKED, record)
                    self.deps.store.mark_blocked(
                        self.run_id,
                        node.node_id,
                        st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
                        detail={
                            "candidate_sha": candidate.candidate_sha,
                            "reason": reason,
                        },
                        retry_class=st.RetryClass.ENVIRONMENTAL,
                    )
                    return False
                replacement_generation = self._active_actor_generation(
                    node.node_id, "reviewer", record
                )
                if replacement_generation > reviewer_generation:
                    # The callback returned from a superseded reviewer. Move
                    # the unfinished durable dispatch to the replacement and
                    # call it again there. Never apply the old callback's
                    # evidence under the replacement generation. A closed
                    # reviewer leaves the builder attempt as the fallback
                    # generation; that lower number is absence, not a
                    # replacement, and must never move this fence backward.
                    recovered = self.deps.store.recover_review_dispatch(
                        self.run_id,
                        review_node.node_id,
                        candidate.candidate_sha,
                        expected_reviewer_generation=reviewer_generation,
                        reviewer_generation=replacement_generation,
                    )
                    durable_review = recovered.review
                    reviewer_generation = replacement_generation
                    if durable_review.state in {
                        st.CandidateReviewState.PUBLISHED,
                        st.CandidateReviewState.DISPATCHED,
                    }:
                        resume_existing_dispatch = True
                        continue
                    break
                review = callback_review
                break
            if review is not None:
                # A returned candidate-bound report is itself durable
                # submission evidence for custom/offline reviewers. The
                # production callback records this immediately after Herdr's
                # prompt proof; this idempotent edge closes the faster-report
                # case before either terminal CAS.
                durable_review = self.deps.store.mark_review_dispatched(
                    self.run_id,
                    review_node.node_id,
                    candidate.candidate_sha,
                    reviewer_generation=reviewer_generation,
                )
                digest, receipt_path, findings = self._review_evidence(review)
                if bool(getattr(review, "passed", False)):
                    durable_review = self.deps.store.complete_review(
                        self.run_id,
                        review_node.node_id,
                        candidate.candidate_sha,
                        reviewer_generation=reviewer_generation,
                        verdict=st.ReviewVerdict.PASS,
                        review_digest=digest,
                        receipt_path=receipt_path,
                        findings=findings,
                    ).review
                else:
                    durable_review = self.deps.store.reject_and_create_handoff(
                        self.run_id,
                        review_node.node_id,
                        candidate.candidate_sha,
                        reviewer_generation=reviewer_generation,
                        builder_generation=repair_builder_generation,
                        review_digest=digest,
                        receipt_path=receipt_path,
                        findings=findings,
                    ).review
        if durable_review.verdict is st.ReviewVerdict.PASS:
            self._require_actor_generation(
                node.node_id, "builder", repair_builder_generation, record
            )
            if not self._review_is_accepted(review_node.node_id):
                self.deps.store.mark_review_accepted(
                    self.run_id, review_node.node_id, candidate.candidate_sha
                )
            self._set_lane_phase(node.node_id, st.LanePhase.ACCEPTED, record)
            self._close_persistent_reviewer(node.node_id, record)
            self._settle_context(context)
            return True
        if durable_review.verdict is not st.ReviewVerdict.REJECTED:
            self._set_lane_phase(
                node.node_id, st.LanePhase.WAITING_FOR_NEW_CANDIDATE, record
            )
            return False
        # Read before this rejection's spend is written, so the window is
        # the prior rejections (§7.5 refusal repetition).
        current_refusal = _review_refusal(
            durable_review.findings, candidate.candidate_sha
        )
        repeats = self._review_refusal_repeats(
            node.node_id, review_node.node_id, candidate.candidate_sha, current_refusal
        )
        self._review_findings[node.node_id] = "\n".join(
            str(finding.get("message", "")) for finding in durable_review.findings
        )

        self._set_lane_phase(node.node_id, st.LanePhase.REPAIR_HANDOFF, record)
        allowed = replayed_handoff or self._lane_retry(
            node,
            st.LaneRetryClass.TEST_REVIEW_REJECTION
            if node.kind is st.NodeKind.TESTS
            else st.LaneRetryClass.REVIEW_REJECTION,
            candidate_sha=candidate.candidate_sha,
            detail={
                "candidate_sha": candidate.candidate_sha,
                "review_digest": durable_review.review_digest,
            },
        )
        guidance = self._refresh_lane_guidance(node.node_id, record)
        if not allowed or repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
            self._close_persistent_reviewer(node.node_id, record)
            self._settle_context(context)
            self._set_lane_phase(node.node_id, st.LanePhase.BLOCKED, record)
            detail: Dict[str, Any] = {
                "candidate_sha": candidate.candidate_sha,
                "findings": list(durable_review.findings),
                # Which budget ran out, named in the detail rather than in
                # a second BlockReason. The operator escape is identical
                # for both -- grant this lane more attempts -- so a second
                # terminal vocabulary entry would be two names for one
                # exit, while an operator still needs to know whether the
                # loop that failed to converge was the tester's or the
                # builder's.
                "retry_class": (
                    st.LaneRetryClass.TEST_REVIEW_REJECTION.value
                    if node.kind is st.NodeKind.TESTS
                    else st.LaneRetryClass.REVIEW_REJECTION.value
                ),
            }
            if repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
                # The reviewer's located findings are byte-identical to the
                # ones the previous candidate was rejected on, so the repair
                # prompt this rejection would produce is the prompt that
                # already failed. Named ahead of the budget reason because it
                # is the truer cause: this lane did not run out of attempts,
                # it stopped moving, and "budget exhausted" would send an
                # operator to grant more of exactly what was not the problem
                # (§7.5).
                reason = st.BlockReason.SEMANTIC_REFUSAL_REPEATED
                detail = rp.repeated_refusal_detail(detail, current_refusal, repeats)
            else:
                reason = st.BlockReason.REVIEW_BUDGET_EXHAUSTED
            self.deps.store.mark_blocked(
                self.run_id, node.node_id, reason, detail=detail
            )
            return False
        repair_basis = rp.RepairBasis(
            base_sha=candidate.candidate_sha,
            integration_head=head,
            repair_of_attempt=record.attempt_no,
            chain_length=candidate.candidate_seq + 1,
        )
        repair_prompt = rp.render_guidance(node, guidance, repair=repair_basis)
        if repair_prompt is None:
            repair_prompt = self._repair_prompt(candidate.candidate_sha, durable_review)
        wt.prepare_descendant_candidate(attempt, candidate.candidate_sha)
        repair_baseline = wt.take_baseline(attempt)
        repair = self._continue_repair_handoff(
            node,
            context,
            attempt,
            record,
            repair_prompt,
            candidate.candidate_sha,
            repair_builder_generation,
        )
        if repair is None:
            return False
        self._complete_attempt(
            node,
            context,
            attempt,
            record,
            rp.RepairBasis(
                base_sha=candidate.candidate_sha,
                integration_head=head,
                repair_of_attempt=record.attempt_no,
                chain_length=candidate.candidate_seq + 1,
            ),
            _pending_gate(),
            repair_baseline,
            repair.execution,
            head,
            record_result=False,
        )
        return False

    # ── settling a failed attempt ───────────────────────────────────────────

    def _record_result(
        self, node: st.PlanNode, record: st.AttemptRecord, execution: NodeExecution
    ) -> None:
        """§7.7 — land the agent's declared result in one adjudicated row.

        **Placed here, before `_require_running`, and that ordering is the
        mechanism rather than an accident.** §7.7 exists for "a reclaimed
        attempt's late result", and the only way one is produced is an attempt
        whose envelope arrives after its generation stopped being the live
        one. Recording after the ownership check would drop exactly the
        payload the section was written to preserve — the correct FAIL
        carrying two real findings that vanished behind a byte-identical
        journal — and would leave `SUPERSEDED` unreachable, so three of the
        four adjudications would be decoration.

        The adjudication is **not** made here. `verification.adjudicate_result`
        owns it and judges the result solely against the attempt row it names,
        never against the node's current state (§7.7); this supplies the row
        and stores what it returns. The write is unconditional on the verdict
        because the payload is retained in all four outcomes.

        Silent on absence rather than refusing: a code node has no envelope
        and an agent whose envelope did not parse has no payload, and neither
        is a result that failed to land — there was none. `ResultRecord`
        refuses to exist without a payload, so the guard is a precondition of
        constructing one, not a policy choice made here.
        """
        payload = execution.envelope_payload
        if payload is None:
            return
        # A watchdog fence is in-memory. Adjudication reads attempts.state.
        # If this generation is convicted but fail() has not released RUNNING
        # yet, a result recorded now is ACCEPTED; declared_result_observed then
        # defers NODE_TIMEOUT to backstop_t_s, and the worker cannot settle
        # because _require_running raises on the fence (issue #48). Release
        # first so the late result is SUPERSEDED — the case §7.7 exists for.
        with self._lock:
            fenced = record.key in self._watchdog_fences
        if fenced and self._owns_running(record):
            self._settle_failure(
                node,
                rp.Classification(
                    retry_class=st.RetryClass.ENVIRONMENTAL,
                    reason=wd.StallReason.NODE_TIMEOUT.value,
                ),
                record=record,
                allow_watchdog_fence=True,
            )
        adjudged = vf.adjudicate_result(
            st.ResultRecord(
                node_id=node.node_id,
                attempt_no=record.attempt_no,
                subject_sha=record.base_sha,
                payload=payload,
            ),
            self.deps.store.attempts_for(self.run_id, node.node_id),
        )
        self.deps.store.record_result(self.run_id, adjudged)

    def _unreferenced_produced_symbols(
        self,
        attempt: "wt.AttemptWorktree",
        baseline: "wt.Inventory",
        measured: "wt.InventoryDelta",
        after: "wt.Inventory",
    ) -> Tuple["rc.ProducedSymbol", ...]:
        """The counted fact behind #118, measured over this attempt's delta.

        Three sources, each read from where it is authoritative: what the
        attempt wrote (the worktree), what stood there before it (the baseline
        inventory's blob, so a module the attempt merely touched is not
        adjudicated for bloat that predates the run), and the surface a
        reference may come from — every Python path in git's universe for this
        worktree, production and test alike.

        A changed file whose base blob is not text is skipped entirely rather
        than treated as new. Treating it as new would attribute every symbol in
        it to this attempt, which is the false refusal this check must not
        manufacture — and a base git could not read at all is not answered here
        at all: `blob_text` raises, because §7.5 forbids reading a failed git
        call as a fact about the tree.
        """
        written = tuple(
            rel for rel in measured.added + measured.changed if rel.endswith(".py")
        )
        if not written:
            return ()
        produced: Dict[str, str] = {}
        base_sources: Dict[str, str] = {}
        for rel in written:
            entry = baseline.get(rel)
            if entry is not None:
                prior = wt.blob_text(attempt.path, entry[1])
                if prior is None:
                    continue
                base_sources[rel] = prior
            current = _read_source(attempt.path / rel)
            if current is not None:
                produced[rel] = current
        if not produced:
            return ()
        surface: Dict[str, str] = dict(produced)
        for rel in after:
            if rel in surface or not rel.endswith(".py"):
                continue
            source = _read_source(attempt.path / rel)
            if source is not None:
                surface[rel] = source
        return rc.unreferenced(produced, surface, base_sources)

    def _settle_verdict(
        self,
        node: st.PlanNode,
        verdict: "vf.VerificationVerdict",
        execution: NodeExecution,
        record: st.AttemptRecord,
        extra_facts: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Turn a failed VERIFIED predicate into a classification (§7.5).

        `extra_facts` is merged onto the failing attempt's row beside the
        guidance entry. Two callers pass it. A falsification refusal carries
        the sealed output commit that recovery can prove; an empty repair
        carries `REPAIR_DIFF_EMPTY_KEY` for durable audit and retry guidance.
        """
        if verdict.block_reason is not None:
            self._settle_failure(
                node,
                rp.Classification(block_reason=verdict.block_reason),
                record=record,
            )
            return

        classification = self._verdict_classification(node, verdict, execution)
        # The adapter's own account of the execution, attached where every
        # other observation is attached. `_with_reason` never overwrites, and
        # neither arm above sets one, so this is the ledger's only chance to
        # record what the launcher saw (§7.6).
        self._settle_failure(
            node,
            _with_reason(classification, execution.launch_detail or None),
            verdict,
            record,
            extra_facts=extra_facts,
        )

    def _mark_lane_blocked(
        self,
        node: st.PlanNode,
        record: st.AttemptRecord,
        *,
        allow_watchdog_fence: bool = False,
    ) -> None:
        """Persist terminal lane phase before the owning node is blocked."""
        if self._review_for_build(node.node_id) is None:
            return
        phase = self.deps.store.get_node(self.run_id, node.node_id).lane_phase
        if phase not in st.LANE_PHASE_TERMINAL:
            self._set_lane_phase(
                node.node_id,
                st.LanePhase.BLOCKED,
                record,
                allow_watchdog_fence=allow_watchdog_fence,
            )

    def _settle_failure(
        self,
        node: st.PlanNode,
        classification: rp.Classification,
        verdict: Optional["vf.VerificationVerdict"] = None,
        record: Optional[st.AttemptRecord] = None,
        allow_watchdog_fence: bool = False,
        extra_facts: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Block or release only the generation that still owns RUNNING."""
        if record is None:
            return
        store = self.deps.store
        with self._lock:
            if (
                self._cancelled.is_set()
                or not self._owns_running(record)
                or (record.key in self._watchdog_fences and not allow_watchdog_fence)
            ):
                return
            # Why the attempt failed, computed once and written wherever this
            # failure is recorded — the retry row below and, above all, the
            # block rows here. A block is terminal, so its transition is the
            # last chance the ledger has to say what failed.
            detail = _failure_detail(classification, verdict)

            if classification.block_reason is not None:
                self._mark_lane_blocked(
                    node, record, allow_watchdog_fence=allow_watchdog_fence
                )
                store.mark_blocked(
                    self.run_id,
                    node.node_id,
                    classification.block_reason,
                    detail=detail or None,
                )
                return

            retry_class = classification.retry_class or st.DEFAULT_RETRY_CLASS
            if self._review_for_build(node.node_id) is not None:
                lane_retry_class = st.LaneRetryClass(retry_class.value)
                candidates = store.lane_candidates(
                    self.run_id, node.node_id, limit=10_000
                )
                candidate_sha = candidates[-1].candidate_sha if candidates else None
                extra = None
                current = None
                repeats = 1
                if st.mutates_prompt(retry_class):
                    extra = dict(rp.guidance_extra_verification(detail))
                    if extra_facts:
                        extra.update(extra_facts)
                    # Read before `_lane_retry` writes this failure's spend,
                    # so the history compared against is the prior refusals.
                    current = rp.verification_guidance(detail)
                    repeats = self._lane_refusal_repeats(node.node_id, current)
                allowed = self._lane_retry(
                    node,
                    lane_retry_class,
                    candidate_sha=candidate_sha,
                    detail=detail,
                    launcher_failure=classification.launcher_failure,
                )
                self._refresh_lane_guidance(node.node_id, record)
                if repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
                    # The refusal this attempt just produced is identical, as
                    # a typed record, to the one its previous SEMANTIC attempt
                    # produced. The spend above already recorded it; what
                    # stops here is the re-dispatch, because the inputs are
                    # proven unchanged — a deduped ledger renders the same
                    # prompt — and re-running them reproduces the refusal
                    # until the budget is gone (§7.5). Checked ahead of the
                    # budget block below because it is the truer cause: a
                    # budget reason tells the operator to grant attempts that
                    # this record proves would refuse identically again.
                    self._mark_lane_blocked(
                        node, record, allow_watchdog_fence=allow_watchdog_fence
                    )
                    store.mark_blocked(
                        self.run_id,
                        node.node_id,
                        st.BlockReason.SEMANTIC_REFUSAL_REPEATED,
                        detail=rp.repeated_refusal_detail(detail, current, repeats),
                        retry_class=retry_class,
                        attempt_extra=extra,
                    )
                    return
                if not allowed:
                    self._mark_lane_blocked(
                        node, record, allow_watchdog_fence=allow_watchdog_fence
                    )
                    block_detail = (
                        self._lane_semantic_budget_detail(detail, node.node_id)
                        if lane_retry_class is st.LaneRetryClass.SEMANTIC
                        else detail
                    )
                    store.mark_blocked(
                        self.run_id,
                        node.node_id,
                        _budget_reason(retry_class, classification.launcher_failure),
                        detail=block_detail or None,
                        retry_class=retry_class,
                        attempt_extra=extra,
                    )
                    return
                store.fail_attempt(
                    self.run_id,
                    node.node_id,
                    retry_class,
                    detail=detail or None,
                    attempt_extra=extra,
                )
                return

            # `st.mutates_prompt`, not an inline `is SEMANTIC`: §7.5's rule
            # about which class rewrites the agent's instructions was written
            # twice, once as that predicate and once as this branch, and two
            # representations of one rule is the RC1 shape this design
            # convicts. The predicate owns the rule; this owns acting on it.
            extra = None
            if st.mutates_prompt(retry_class):
                # memory, because memory does not survive the process and the
                # next attempt may be dispatched by a different one.
                extra = dict(rp.guidance_extra_verification(detail))
                if extra_facts:
                    extra.update(extra_facts)
                current = rp.verification_guidance(detail)
                # Read before this failure's own guidance extra lands on its
                # attempt row below, so the history is the prior refusals.
                repeats = self._attempt_refusal_repeats(record, current)
                self._guidance[record.guidance_key] = self._guidance.get(
                    record.guidance_key, rp.GuidanceLedger()
                ).with_verification(current)
                lifecycle = store.get_node(self.run_id, node.node_id)
                if repeats >= rp.IDENTICAL_REFUSAL_LIMIT:
                    # Identical typed refusal to the previous SEMANTIC attempt
                    # at this guidance key: the inputs are proven unchanged
                    # (the deduped ledger renders the same prompt), so a
                    # re-dispatch reproduces the refusal until the budget is
                    # gone (§7.5). The blocking attempt persists its guidance
                    # the same way the ceiling block below does.
                    self._mark_lane_blocked(
                        node, record, allow_watchdog_fence=allow_watchdog_fence
                    )
                    store.mark_blocked(
                        self.run_id,
                        node.node_id,
                        st.BlockReason.SEMANTIC_REFUSAL_REPEATED,
                        detail=rp.repeated_refusal_detail(detail, current, repeats),
                        retry_class=retry_class,
                        attempt_extra=extra,
                    )
                    return
                if self._semantic_ceiling_reached(
                    node.node_id, lifecycle.granted_extra_attempts
                ):
                    # The capped attempt persists its guidance too. A node
                    # blocked on the ceiling is the one most likely to be
                    # resumed after an operator grants it more attempts, and
                    # it is exactly the finding that would have been lost.
                    self._mark_lane_blocked(
                        node, record, allow_watchdog_fence=allow_watchdog_fence
                    )
                    store.mark_blocked(
                        self.run_id,
                        node.node_id,
                        st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
                        detail=self._semantic_budget_detail(
                            detail, node.node_id, lifecycle.granted_extra_attempts
                        ),
                        retry_class=retry_class,
                        attempt_extra=extra,
                    )
                    return
            else:
                # LAUNCHER_TRANSIENT's budget is a property of the member, not
                # the class: §7.5 gives CREDENTIAL zero and the rest one or
                # two. `config.retry_budget` cannot express that, so asking it
                # alone gave a credential refusal the same two retries as a
                # dropped transport — the zero-retry rule stated in §7.5 and
                # implemented in `launcher_retry_budget` had no caller.
                budget = (
                    rp.launcher_retry_budget(
                        self.config, classification.launcher_failure
                    )
                    if retry_class is st.RetryClass.LAUNCHER_TRANSIENT
                    else self.config.retry_budget(retry_class)
                )
                spent = store.attempts_spent(self.run_id, node.node_id, retry_class)
                if spent >= budget:
                    self._mark_lane_blocked(
                        node, record, allow_watchdog_fence=allow_watchdog_fence
                    )
                    store.mark_blocked(
                        self.run_id,
                        node.node_id,
                        _budget_reason(retry_class, classification.launcher_failure),
                        detail=detail or None,
                        retry_class=retry_class,
                    )
                    return

            store.fail_attempt(
                self.run_id,
                node.node_id,
                retry_class,
                detail=detail or None,
                attempt_extra=extra,
            )

    def _semantic_budget_detail(
        self, detail: Mapping[str, Any], node_id: str, granted: int
    ) -> Dict[str, Any]:
        """The block payload for `SEMANTIC_BUDGET_EXHAUSTED`, with the numbers
        an operator needs to size the escape (#81).

        Everything the block reason carried before this said *that* a budget
        ran out. Reopening the node needs to know by how much it overran, and
        the count is cumulative across every base for the `(run_id, node_id)`
        pair — so it is not the per-process series `review_convergence`
        reports, and it was not recoverable from anything the run printed. One
        real node blocked with seven spent attempts while the run report
        printed a convergence series of `[3]`, because only three of the seven
        happened in the process that reported.

        `semantic_grant_required` is the arithmetic rather than the inputs to
        it, deliberately: the count this decision reads excludes the attempt
        currently being blocked, whose row `mark_blocked` writes in the same
        transaction, so an operator reading the raw count off the payload and
        sizing a grant from it would be short by exactly one — the off-by-one
        `_semantic_ceiling_reached` documents, moved onto the operator. It is
        the smallest `retry --grant N` under which this node can absorb one
        further failure instead of re-blocking on it.

        It lived on `REVIEW_BUDGET_EXHAUSTED` until §19 M35 removed that block
        entirely. The payload moved rather than being deleted with it: the
        ceiling an operator now has to size an escape against is this one, and
        deleting the only place its magnitude was ever reported would have
        re-opened #81 while closing something else.
        """
        already = rp.semantic_attempts_total(
            self.deps.store.attempts_for(self.run_id, node_id), node_id
        )
        # After the block, `already + 1` rows are stored. The next failure
        # blocks again unless `stored + 1 < ceiling + granted`.
        required = (already + 1) + 2 - self.config.semantic_ceiling - granted
        return dict(
            detail,
            semantic_attempts_total=already,
            semantic_ceiling=self.config.semantic_ceiling,
            granted_extra_attempts=granted,
            semantic_grant_required=max(1, required),
        )

    def _lane_refusal_repeats(
        self, node_id: str, current: rp.VerificationGuidance
    ) -> int:
        """§7.5 refusal repetition over a retained lane's floored spends.

        Called *before* the current failure's spend is written, so the
        history is exactly the prior refusals. The floored view is the same
        one the budgets read: an operator boundary (`retry`, `run resume`)
        forgives the identity chain along with the spend, so a granted
        attempt is compared against nothing and always runs.
        """
        history = rp.verification_refusals_from_spends(
            self.deps.store.current_lane_retry_spends(
                self.run_id, node_id, limit=10_000
            )
        )
        return rp.refusal_repetition(history, current)

    def _attempt_refusal_repeats(
        self, record: st.AttemptRecord, current: rp.VerificationGuidance
    ) -> int:
        """§7.5 refusal repetition over durable fresh-attempt guidance rows.

        Scoped by `record.guidance_key` — a refusal is evidence about a tree,
        and an upstream merge that moves the integration head is a genuine
        change of inputs that ends the repetition claim. Floored on the same
        operator boundary the attempt budgets honour (§11.3). The current
        failure's own guidance extra is not yet written when this runs, so
        the history is exactly the prior refusals.
        """
        store = self.deps.store
        history = rp.verification_refusals_from_attempts(
            store.attempts_for(self.run_id, record.node_id),
            record.guidance_key,
            floor=store.retry_spend_floor(self.run_id, record.node_id),
        )
        return rp.refusal_repetition(history, current)

    def _review_refusal_repeats(
        self,
        node_id: str,
        review_node_id: str,
        candidate_sha: str,
        current: rp.VerificationGuidance,
    ) -> int:
        """§7.5 refusal repetition over a lane's floored review rejections.

        The review half of `_lane_refusal_repeats`, and the same predicate.
        Only the history differs, because a rejection is durable on the
        candidate review row rather than in a spend's detail. The window is
        still the floored lane spends, so the operator boundary the budgets
        honour is honoured here too: `retry --force` forgives the identity
        chain along with the spend, and a replayed handoff after one is
        compared against nothing rather than re-blocking before dispatch.

        The current rejection is excluded by its candidate sha. Unlike the two
        verification sites, this runs *after* the refusal is durable, and a
        replay re-enters on the very row being compared.
        """
        store = self.deps.store
        window = {
            spend.candidate_sha
            for spend in store.current_lane_retry_spends(
                self.run_id, node_id, limit=10_000
            )
            if spend.retry_class in _REVIEW_REJECTION_CLASSES
        }
        history = [
            _review_refusal(review.findings, review.candidate_sha)
            for review in store.candidate_reviews(
                self.run_id, review_node_id, limit=10_000
            )
            if review.verdict is st.ReviewVerdict.REJECTED
            and review.candidate_sha != candidate_sha
            and review.candidate_sha in window
        ]
        return rp.refusal_repetition(history, current)

    def _semantic_ceiling_reached(self, node_id: str, granted: int) -> bool:
        """Enforce the cumulative semantic ceiling across every attempt base.

        Operator boundaries refresh infrastructure tolerance, not semantic
        adjudication. Semantic recovery requires an explicit grant. The
        in-flight failure is not classified yet and contributes the `+ 1`.
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
            records = self._merge_records(self.deps.store.node_records(self.run_id))
            candidate = wt.merge_ready(records)
            if candidate is None:
                return
            candidate_node = self.nodes.get(candidate.node_id)
            if self._is_derived_review(candidate_node):
                return
            review = self._review_for_build(candidate.node_id)
            with self._lock:
                if self._cancelled.is_set():
                    return
                output_sha = self._output_shas.get(candidate.node_id)
                lifecycle = self.deps.store.get_node(self.run_id, candidate.node_id)
                if output_sha is None:
                    return
                if review is not None:
                    if (
                        lifecycle.lane_phase is not st.LanePhase.ACCEPTED
                        or not self._review_is_accepted(review.node_id)
                    ):
                        return
                    publication = self.deps.store.candidate(
                        self.run_id, candidate.node_id, output_sha
                    )
                    completed = self.deps.store.candidate_review(
                        self.run_id, review.node_id, output_sha
                    )
                    if (
                        publication is None
                        or completed is None
                        or completed.verdict is not st.ReviewVerdict.PASS
                    ):
                        return
                if not self._merge_pairing_proven(candidate.node_id, output_sha):
                    return

            result = wt.merge_verified_node(
                Path(self.deps.integration_path), candidate.node_id, output_sha
            )
            if result.conflicted_paths:
                # §8.7 — capture, abort, block with the evidence, and let the
                # descendants become derived-unready. Resolution is human: a
                # conflict means two output sets overlapped in content though
                # their declared globs did not, which is a planning defect
                # that re-prompting papers over.
                self.deps.store.mark_blocked(
                    self.run_id,
                    candidate.node_id,
                    st.BlockReason.MERGE_CONFLICT,
                    detail={"conflicted_paths": list(result.conflicted_paths)},
                )
                continue
            if not result.ancestry_proven:
                self.deps.store.mark_blocked(
                    self.run_id,
                    candidate.node_id,
                    st.BlockReason.MERGE_CONFLICT,
                    detail={"reason": "ancestry not proven after merge"},
                )
                continue
            if self._cancelled.is_set():
                return
            self.deps.store.mark_merged(self.run_id, candidate.node_id)
            self._remove_merged_worktree(candidate.node_id, result.ancestry_proven)

    def _merge_pairing_proven(self, node_id: str, output_sha: str) -> bool:
        """The merge check for test/implementation pairing.

        Two directions, because the pair has two halves and each can be wrong
        on its own:

        * a **tests** node merges only when the sha about to be merged is the
          one that carries strong evidence and a passed review. A tests node
          whose accepted candidate is some *other* sha would otherwise merge
          bytes nobody measured;
        * an **implementation** node merges only when a pairing row exists for
          exactly `(this node, this implementation sha, that accepted test
          sha)`. That row is written by `_bind_test_pairing` after the tests
          were proven byte-identical and green here, so its presence is the
          durable statement that the exact accepted pair was verified — never
          a re-derivation at merge time from whatever tree is present.

        A legacy-pinned run is not asked either question. Its nodes were
        admitted under the contract they were created with, and adding a merge
        refusal to an in-flight run is precisely the retroactive
        reclassification the rollout invariant forbids.
        """
        if self.test_strength_contract is not st.TestStrengthContract.STRENGTH_V1:
            return True
        if self._is_tests_node(node_id):
            accepted = self.deps.store.accepted_test_candidate(
                self.run_id, node_id)
            return accepted is not None and accepted.candidate_sha == output_sha
        node = self.authored_nodes.get(node_id)
        if node is None:
            return True
        for tests_node in self._tests_prerequisites(node):
            accepted = self.deps.store.accepted_test_candidate(
                self.run_id, tests_node.node_id)
            if accepted is None:
                return False
            paired = self.deps.store.test_pairings(
                self.run_id, node_id, output_sha)
            if not any(
                row.tests_node_id == tests_node.node_id
                and row.accepted_test_sha == accepted.candidate_sha
                for row in paired
            ):
                return False
        return True

    def _remove_merged_worktree(self, node_id: str, ancestry_proven: bool) -> None:
        """§8.8's cleanup for a node that merged with its ancestry proven.

        Nothing removed an attempt worktree before this. `run start`'s
        `finally` releases the run's *integration* checkout and reclaims
        stranded integration checkouts, and both are explicit that attempt
        worktrees are not their business — correctly, since those hold their
        own attempt branches. So every attempt of every node left a checkout
        and a branch behind, growing with the run rather than with the graph.

        Three properties of §8.8 are kept exactly:

        * **Only after ancestry is proven.** Deleting an unmerged branch
          destroys the only copy of the work, and `remove_attempt_worktree`
          refuses rather than warns. The flag is the merge's own measured
          answer, passed through rather than re-derived.
        * **Blocked nodes' worktrees are retained for post-mortem** — by never
          reaching here, which is the mechanism that function's docstring
          names.
        * **Nothing forces.** `git worktree remove` and `git branch -d` are
          both allowed to refuse, and a refusal is reported rather than
          overridden: a worktree that will not come away cleanly is evidence
          about the attempt.

        The refusal is reported and not raised. This runs on the merge loop
        thread, where an exception would abandon the remaining frontier and
        turn a leaked directory into a lost run — a cleanup failure must not
        be more destructive than the leak it is cleaning up. It lands on the
        same operator-visible surface as §8.3's hygiene report.
        """
        with self._lock:
            attempt = self._attempt_worktrees.pop(node_id, None)
        if attempt is None:
            return
        try:
            wt.remove_attempt_worktree(
                attempt,
                ancestry_proven=ancestry_proven,
                integration_path=Path(self.deps.integration_path),
            )
        except (wt.WorktreeError, OSError) as exc:
            self._report_hygiene(
                node_id,
                (
                    "attempt worktree {0} was not removed: {1}".format(
                        attempt.path, exc
                    ),
                ),
            )

    def _report_hygiene(self, node_id: str, entries: Tuple[str, ...]) -> None:
        """Add harness-hygiene facts about one node without losing the others."""
        if not entries:
            return
        with self._lock:
            self._adapter_hygiene[node_id] = (
                self._adapter_hygiene.get(node_id, ()) + entries
            )

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
        merged = tuple(
            sorted(
                node_id
                for node_id, state in states.items()
                if (
                    state == st.NodeState.MERGED.value
                    and not self._is_derived_review(self.nodes.get(node_id))
                )
            )
        )

        acceptance = None
        if not self._cancelled.is_set() and self._is_candidate_accepted(states):
            acceptance = self._run_final_acceptance(records, merged)
        with self._lock:
            if self._cancelled.is_set():
                store.cancel_run(self.run_id)
                records = store.node_records(self.run_id)
                states = {r.node_id: r.state for r in records}
                merged = tuple(
                    sorted(
                        node_id
                        for node_id, state in states.items()
                        if (
                            state == st.NodeState.MERGED.value
                            and not self._is_derived_review(self.nodes.get(node_id))
                        )
                    )
                )
            cancelled = tuple(
                sorted(
                    n for n, s in states.items() if s == st.NodeState.CANCELLED.value
                )
            )
            blocked = tuple(
                sorted(
                    (n, store.get_node(self.run_id, n).block_reason)
                    for n, s in states.items()
                    if s == st.NodeState.BLOCKED.value
                )
            )
            # This closes the cancellation/ACCEPTED race: after the final gate
            # returned, either cancellation is made durable here or acceptance
            # is declared before a later cancellation request can take effect.
            declared = store.declare_outcome(
                self.run_id,
                stuck=self._stuck,
                acceptance_result=(acceptance.green if acceptance else None),
            )

        return RunReport(
            outcome=declared.outcome,
            merged=merged,
            blocked=blocked,
            abandoned=cancelled,
            upstream_blocked=store.upstream_blocked(self.run_id),
            acceptance=acceptance,
            ancestry=dict(acceptance.ancestry) if acceptance else {},
            adapter_hygiene=dict(self._adapter_hygiene),
            # Every node a reviewer rejected, not only the blocked ones. The
            # filter belonged to a world where a rejection was what blocked a
            # node, so a rejected node was necessarily a blocked one; since
            # §19 M35 the common case is a rejected node that merged, and
            # dropping its findings here would be the only place they were
            # ever going to be read from.
            review_findings=dict(self._review_findings),
            # Projected here rather than accumulated in memory. A dict the
            # scheduler appended to on every rejection and `project` rebuilt
            # from the store on every resume was a second representation of
            # what `candidate_reviews` already holds, and the two could only
            # ever agree by copying (§4).
            review_convergence={
                node_id: tuple(counts)
                for node_id, counts in rp.review_convergence_from_reviews(
                    store.candidate_reviews(self.run_id, limit=10_000)
                ).items()
            },
            review_nodes={
                node_id: state
                for node_id, state in states.items()
                if self._is_derived_review(self.nodes.get(node_id))
            },
        )

    def _is_candidate_accepted(self, states: Mapping[str, str]) -> bool:
        """Every source merge needs an accepted review edge before final gate."""
        source_merged = any(
            state == st.NodeState.MERGED.value
            and not self._is_derived_review(self.nodes.get(node_id))
            for node_id, state in states.items()
        )
        return source_merged and all(
            state in (st.NodeState.MERGED.value, st.NodeState.CANCELLED.value)
            or (
                state == st.NodeState.ACCEPTED.value
                and self._is_derived_review(self.nodes.get(node_id))
            )
            # A review row this run's contract excludes holds nothing back.
            # It is a ledger artefact of a runtime that projected it under
            # rules this run was never created with, and asking a legacy run
            # to reach a terminal state on it would strand the run forever on
            # a node nothing will ever dispatch (§19 M42).
            or self._out_of_contract_review_owner(node_id) is not None
            for node_id, state in states.items()
        )

    def _run_final_acceptance(
        self, records: Sequence["wt.NodeRecord"], merged: Sequence[str]
    ) -> AcceptanceResult:
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
                green=False, specs=(), reason="cancellation requested before acceptance"
            )
        store.acceptance_started(self.run_id)
        deadline = self._time_source() + self.config.final_acceptance_timeout_s

        with self._lock:
            shas = {n: self._output_shas[n] for n in merged if n in self._output_shas}
        ancestry = wt.final_ancestry_sweep(Path(self.deps.integration_path), shas)
        if self._cancelled.is_set():
            return AcceptanceResult(
                green=False,
                specs=(),
                ancestry=ancestry,
                reason="cancellation requested during the final ancestry sweep",
            )
        if not all(ancestry.values()) or len(ancestry) != len(merged):
            return AcceptanceResult(
                green=False,
                specs=(),
                ancestry=ancestry,
                reason="the final ancestry sweep did not re-prove every merged node",
            )
        if self._time_source() > deadline:
            return AcceptanceResult(
                green=False,
                specs=(),
                ancestry=ancestry,
                reason="final-acceptance timeout during the sweep",
            )

        specs = wt.acceptance_specs(records)
        try:
            gate = self.deps.run_integration_gate(
                Path(self.deps.integration_path), specs, self._cancelled.is_set
            )
        except wt.GateCancelled:
            return AcceptanceResult(
                green=False,
                specs=specs,
                ancestry=ancestry,
                reason="cancellation requested during the final integration gate",
            )
        if self._cancelled.is_set():
            return AcceptanceResult(
                green=False,
                specs=specs,
                gate=gate,
                ancestry=ancestry,
                reason="cancellation requested during the final integration gate",
            )
        verdict = vf.adjudicate_gate(gate, self.deps.integration_min_cases)
        if self._time_source() > deadline:
            return AcceptanceResult(
                green=False,
                specs=specs,
                gate=gate,
                ancestry=ancestry,
                reason="final-acceptance timeout during the gate",
            )
        return AcceptanceResult(
            green=verdict.green,
            specs=specs,
            gate=gate,
            ancestry=ancestry,
            reason=verdict.reason,
        )


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
            f"fail to create. Choose a name outside {prefix}/."
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def _pending_gate() -> "vf.GateVerdict":
    """A stand-in for clause 3 before the commit exists.

    Green, because at this point in the order clause 3 has not been evaluated
    and must not be allowed to fail the conjunction early — the real
    evaluation happens against the committed tree a few lines later. Named
    rather than inlined so that no reader mistakes it for a gate result.
    """
    return vf.GateVerdict(
        green=True, unparseable=False, counts=None, reason="clause 3 not yet evaluated"
    )


def _is_merged(store, run_id: str, node_id: str) -> bool:
    return any(
        r.node_id == node_id and r.state == st.NodeState.MERGED.value
        for r in store.node_records(run_id)
    )


#: The lane retry classes a reviewer's rejection spends. Both name the same
#: event -- a candidate refused on its findings -- so both bound the window
#: inside which "the reviewer has said this before" holds.
_REVIEW_REJECTION_CLASSES = (
    st.LaneRetryClass.REVIEW_REJECTION,
    st.LaneRetryClass.TEST_REVIEW_REJECTION,
)

#: Stamped on the refusal built from a rejection so `rp.refusal_repetition`
#: admits it at all: that predicate refuses an identity claim from any surface
#: that has not declared its refusal deterministic. Independent node review is
#: §1.2's one typed semantic adjudication boundary, and the immutable,
#: candidate-SHA-bound findings it rejects with are that declaration.
_REVIEW_REFUSAL_CODE = "REVIEW_REJECTED"


#: Stands in for the reviewed candidate's sha wherever a finding quotes it.
#: `review_objects` names the diff object `diff:<output_sha>`, so the object
#: id of every `diff.*` finding -- which is most of the blocking ones --
#: differs on every round *by construction*, for the same reason
#: `review_digest` does: the sha is what changed. Left in, it would make two
#: byte-identical verdicts unequal and the check could never fire; taken out,
#: what remains is the rubric cell, the object's identity, the grade, and
#: what the reviewer said about it.
_CANDIDATE_PLACEHOLDER = "<candidate>"


def _review_refusal(
    findings: Sequence[Mapping[str, Any]], candidate_sha: str
) -> rp.VerificationGuidance:
    """One rejection as the typed refusal `rp.refusal_repetition` compares.

    Keyed on the located findings -- rubric cell, object, grade, and the
    message that cell carries -- with the candidate sha masked out of all
    four, and on nothing else. Not `review_digest`, the reviewer generation,
    or a completion time.

    The message is in the key because it *narrows* it. The typed cells alone
    say "the same rubric check failed on the same object again", which is
    what an honest repair loop looks like halfway to converging; stopping on
    that would collapse ordinary iteration to two attempts, which is the
    hazard §7.5 names when it refuses identity to a coarse refusal. Requiring
    the reviewer to have said byte-identically the same thing about the same
    cell is the non-convergence proof, and every field of it is read out of
    the same immutable receipt record the rejection itself is adjudicated
    from -- never from pane text, prompt text, or an agent's claim about its
    own work (§1.2).
    """

    def cell(finding: Mapping[str, Any]) -> str:
        rendered = "|".join(
            # Declared value, not repr: `grade` arrives as the enum on the
            # write path the rejection returns and as its JSON string when the
            # same row is read back, and two spellings of one grade would make
            # every comparison across that boundary unequal.
            str(getattr(finding.get(field, ""), "value", finding.get(field, "")))
            for field in ("check_id", "object_id", "grade", "message")
        )
        return (
            rendered.replace(candidate_sha, _CANDIDATE_PLACEHOLDER)
            if candidate_sha
            else rendered
        )

    return rp.VerificationGuidance(
        reason="\n".join(cell(finding) for finding in findings),
        refusal_code=_REVIEW_REFUSAL_CODE,
    )


def _budget_reason(
    retry_class: st.RetryClass, launcher_failure: Optional[rp.LauncherFailure] = None
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
    if retry_class is st.RetryClass.SEMANTIC:
        return st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED
    if retry_class is st.RetryClass.LAUNCHER_TRANSIENT:
        if launcher_failure is rp.LauncherFailure.CREDENTIAL:
            return st.BlockReason.CREDENTIAL_REFUSED
        if launcher_failure in rp.DETERMINISTIC_LAUNCHER_FAILURES:
            # Same reasoning one member over: the budget was zero, so saying
            # it was exhausted describes a spend that never happened and
            # points the operator at a number instead of at the refusal
            # (§16.3 item 46).
            return st.BlockReason.LAUNCH_REFUSED
        return st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED
    return st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED


#: How long the loop pauses while a node is mid-verdict. Short enough that a
#: settled verdict is picked up promptly, long enough not to spin a core.
_SETTLING_POLL_S = 0.05


def _launch_left_nothing_to_reap(exc: Optional[BaseException]) -> bool:
    """Whether a failed launch typed itself as having created no pane.

    `None` — no exception in flight at all — is not such a statement, so it
    answers `False` and the caller proves absence exactly as it would have.

    Structural on both counts: the exception's *type*, which §7.5 names among
    the facts a classifier may read, and a typed boolean the launcher set. No
    message is inspected — `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:...`
    carries its code in prose and matching that prefix is the shortcut §7.5
    forbids (§16.3 item 45).
    """
    return isinstance(exc, LaunchFailed) and not exc.pane_created


def _with_reason(
    classification: rp.Classification, reason: Optional[str]
) -> rp.Classification:
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


def _stall_signal(stall: BaseException) -> Optional[str]:
    """The reviewer window's own typed account of how it ended, as its value.

    `ReviewStalled.signal` is a `finalization_window.FinalizationSignal`
    member — a closed vocabulary the window owns and this module only reads.
    The `.value` rather than the member so the ledger stores a string it can
    round-trip, and `str()` on anything unexpected rather than a raise: this
    runs inside a failure handler, and a handler that raises on a shape it did
    not anticipate turns a recoverable stall into a lost attempt.
    """
    signal = getattr(stall, "signal", None)
    if signal is None:
        return None
    value = getattr(signal, "value", signal)
    return value if isinstance(value, str) else str(value)


def _check_result_reason(result: "wt.CheckResult") -> str:
    """Which §8.3 check failed, from the check's own typed detail.

    `CheckResult.detail` carries the three git-side checks only, so a
    cleanliness failure — the check with no git symptom at all — arrived at
    the ledger as the bare stage name. §8.3 says a divergence is reported
    "with the paths named", and an operator told only "post-commit check
    failed" has to re-run the attempt to learn which path convicted it. The
    divergences are typed (`Divergence.kind`, `.path`), so they are rendered
    here beside the git detail rather than summarised into prose.

    Bounded, because a pathological delta could otherwise write an unbounded
    row: the count is always stated and the first few paths are named, which
    is enough to identify the writer.
    """
    parts: List[str] = list(result.detail)
    cleanliness = result.cleanliness
    if cleanliness is not None and not cleanliness.clean:
        shown = cleanliness.divergences[:8]
        named = ", ".join("{0} {1}".format(d.kind, d.path) for d in shown)
        parts.append(
            "{0} path(s) diverge from the expected inventory: {1}{2}".format(
                len(cleanliness.divergences),
                named,
                ", ..." if len(cleanliness.divergences) > len(shown) else "",
            )
        )
    if parts:
        return "{0} check failed: {1}".format(result.stage, "; ".join(parts))
    return "{0} check failed".format(result.stage)


def _read_source(path: Path) -> Optional[str]:
    """One file's text, or `None` for anything a parser could not read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _failure_detail(
    classification: rp.Classification, verdict: Optional["vf.VerificationVerdict"]
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
        if verdict.refusal_code:
            # The refusing surface's own typed vocabulary member. `verdict`
            # above starts with the same string, but a prose prefix is not a
            # typed fact: this field is what lets `refusal_repetition` claim
            # two refusals are the same adjudication without reading prose.
            detail["refusal_code"] = verdict.refusal_code
        if verdict.remedy:
            # The other half of the verdict: what would satisfy the refusing
            # check, declared by the surface that refused (deterministic text
            # keyed on the typed code, §1.2). Durable here so a resumed run's
            # retry prompt can still say it, and so a blocked node's last
            # verdict is actionable from the ledger alone.
            detail["remedy"] = verdict.remedy
        if verdict.unreferenced_symbols:
            # What the attempt must delete, located, and durable for the same
            # reason the offending paths are: a node that exhausts its semantic
            # budget over unreachable machinery must leave behind which symbols
            # they were, not a sentence saying there were some.
            detail["unreferenced_symbols"] = list(verdict.unreferenced_symbols)
        if verdict.offending_paths:
            # The paths §7.5 says justify calling a permission failure
            # SEMANTIC. They were named in the retry prompt and nowhere
            # durable, so a node that exhausted its semantic budget blocked
            # without ever recording which paths it wrote.
            detail["offending_paths"] = list(verdict.offending_paths)
    return detail


def _wait_any(
    items: Sequence[Tuple[str, "Future"]], timeout: float = 0.05
) -> Tuple[List[str], List[str]]:
    """Return completed node ids; propagate any escaped worker failure.

    `_attempt` contains ordinary execution failures and writes their durable
    verdicts. A future-level exception is therefore a scheduler defect, not a
    retry signal. Swallowing it leaves a ready PENDING node unchanged and can
    create an unbounded redispatch loop with no transition evidence.
    """
    done = [node_id for node_id, future in items if future.done()]
    if done:
        for node_id, future in items:
            if node_id in done:
                future.result()
        return done, [n for n, _ in items if n not in done]
    for _, future in items:
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            pass
        break
    done = [node_id for node_id, future in items if future.done()]
    for node_id, future in items:
        if node_id in done:
            future.result()
    return done, [n for n, _ in items if n not in done]
