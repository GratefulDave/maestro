"""The scheduler's shared vocabulary — states, outcomes, classes, node model.

Step 6 is three separately-implemented pieces — the lifecycle store, the retry
policy, and the watchdog — plus the worker body that composes them. All four
must agree on the names below *exactly*. Three private copies of one enum,
reconciled by everyone having read the same section, is the RC1 shape (§4)
this design convicts everywhere else; it would drift silently, because no
piece's own tests can see another piece's copy.

So the vocabulary is one module, imported by all of them, and the agreement is
executed in `tests/test_scheduler_types.py` rather than assumed.

What lives here is only what more than one piece needs, plus the two
constructions that must refuse invalid input at the point it is written rather
than at the point it is used:

* `PlanNode`, because §7.4's scope rule and §7.3's code-node clauses are
  structural facts about a node, and a node that violates either is a deadlock
  or an unevaluatable state rather than a runtime error to be handled.
* `SchedulerConfig`, because §11.2's liveness bound is preflight's refusal and
  a scheduler constructed below the bound kills healthy runs.

What deliberately does *not* live here: the outcome function (§7.3), the
classifier (§7.5), and the watchdog's signals (§7.6). Each is behaviour owned
by exactly one piece, and putting behaviour beside a shared vocabulary is how
a vocabulary module becomes a second scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from . import worktree as wt


# ── §7.3 states, and the two kinds of terminal ──────────────────────────────

class NodeState(str, Enum):
    """The six. READY, MERGE_READY, and MERGING are deliberately absent: the
    first is a derived predicate, the second a query, and the third a thing
    recovery asks git about rather than reads off a marker."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VERIFIED = "VERIFIED"
    MERGED = "MERGED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


#: Nothing transitions out of these, ever (§7.3).
ABSOLUTELY_TERMINAL: Tuple[NodeState, ...] = (NodeState.MERGED, NodeState.CANCELLED)

#: No *automatic* transition leaves this, but the operator escapes can (§11.3).
OPERATOR_TERMINAL: Tuple[NodeState, ...] = (NodeState.BLOCKED,)

#: Ends a node without a merge, so the frontier skips it and its descendants
#: become derived-unready (§8.5, §8.7). The merge protocol already owns this
#: set; the tuple below is built *from* it rather than beside it, so the two
#: cannot drift into two representations of one fact.
TERMINAL_WITHOUT_MERGE: Tuple[NodeState, ...] = tuple(
    NodeState(value) for value in wt.TERMINAL_WITHOUT_MERGE)


class RunOutcome(str, Enum):
    """Declared exactly once, at the point the run stops (§7.3)."""

    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    STUCK = "STUCK"


#: The residual class, named rather than enumerated: a combination nobody
#: anticipated lands here with a report, never outside the set (§7.3).
RESIDUAL_OUTCOME = RunOutcome.BLOCKED


class CancelCause(str, Enum):
    """Why a `CANCELLED` was written — the same vocabulary at both levels.

    `CANCELLED` was one word carrying two facts, exactly as "terminal" was
    before §7.3 split it. A run, or a node, reaches `CANCELLED` down one of
    two paths, and they are not alike:

    * `RUN_CANCEL` — the operator ran `run cancel`. The machine was asked to
      stop. Nothing was adjudicated, no result was reached, and there is no
      outcome to protect from being reopened. `run cancel` is the operator's
      only stop control, so this is also the cause written when an operator
      wanted to pause and had no verb that said so.
    * `ABANDONED` — the work itself was given up on, node by node, through
      `abandon` (§11.3). At run level it is the shape §7.3 names "every node
      is CANCELLED": a run stopped deliberately, just piecewise. Each node in
      it was individually adjudicated as work the run should finish without,
      which is a decision about the work and is closer to `ACCEPTED` than to
      a stop request.

    Stored typed, at both levels, because `run resume`'s legality keys on it
    and §1.2 forbids a lifecycle transition keyed on prose. The run-level
    value is an attribute of the *declared outcome* and is rewritten by every
    `declare_outcome`; `runs.cancel_requested` is the separate, live fact that
    a stop was *requested*, consumed by the outcome function as an input and
    cleared by a resume. One is the question put to the scheduler, the other
    is the answer it recorded.
    """

    RUN_CANCEL = "RUN_CANCEL"
    ABANDONED = "ABANDONED"


#: The causes a `CANCELLED` may be reopened from (§7.3, §7.8). `MERGED` and an
#: `ABANDONED` `CANCELLED` remain absolutely terminal; a `RUN_CANCEL`
#: `CANCELLED` is terminal only because the operator asked the machine to
#: stop, and a resume of that same run is the operator withdrawing the
#: request. Membership is data rather than a branch so that the guard, the
#: resume predicate, and the tests read one list.
REOPENABLE_CANCEL_CAUSES: Tuple[CancelCause, ...] = (CancelCause.RUN_CANCEL,)


# ── §7.5 retry classes ──────────────────────────────────────────────────────

class RetryClass(str, Enum):
    """Three, mutually exclusive, classified structurally and never lexically."""

    SEMANTIC = "SEMANTIC"
    ENVIRONMENTAL = "ENVIRONMENTAL"
    LAUNCHER_TRANSIENT = "LAUNCHER_TRANSIENT"


#: Fail-closed. Any exception reaching a worker's top-level handler without a
#: classification is ENVIRONMENTAL, so an engine bug can never be recorded as
#: a verdict about the code under test (§7.5 containment).
DEFAULT_RETRY_CLASS = RetryClass.ENVIRONMENTAL


def mutates_prompt(retry_class: RetryClass) -> bool:
    """Only SEMANTIC rewrites the agent's instructions (§7.5).

    An infra fault must never produce a code verdict, an ownership entry, or
    a budget decrement, and mutating the prompt on one would be all three.
    """
    return retry_class is RetryClass.SEMANTIC


class Escape(str, Enum):
    """The operator's exits from a blocked node (§11.3)."""

    RETRY = "retry"
    RETRY_FORCE = "retry --force"
    SKIP = "skip"
    ABANDON = "abandon"


class EscapeRefusal(str, Enum):
    """Why an escape against a RUNNING node failed closed.

    The race these name is a run-level fact — is the scheduler still a
    process — not a node-state fact. UNKNOWN is not dead.
    """

    SCHEDULER_STILL_ALIVE = "SCHEDULER_STILL_ALIVE"
    SCHEDULER_LIVENESS_UNKNOWN = "SCHEDULER_LIVENESS_UNKNOWN"


class BlockReason(str, Enum):
    """Every *stored* reason a node stopped.

    `UPSTREAM_BLOCKED` is absent on purpose and its absence is load-bearing
    (§8.7): stored, the cascade was irreversible, so a rescue that un-blocked
    the origin left five descendants in a durable terminal state with no rule
    anywhere to bring them back. Derived, the origin leaves the blocked set
    and the predicate simply stops holding.
    """

    GATE_NOT_FALSIFIABLE = "GATE_NOT_FALSIFIABLE"
    CODE_NODE_NO_EFFECT = "CODE_NODE_NO_EFFECT"
    PERMISSION_SCOPE_VIOLATION = "PERMISSION_SCOPE_VIOLATION"
    SEMANTIC_BUDGET_EXHAUSTED = "SEMANTIC_BUDGET_EXHAUSTED"
    REVIEW_BUDGET_EXHAUSTED = "REVIEW_BUDGET_EXHAUSTED"
    ENVIRONMENTAL_BUDGET_EXHAUSTED = "ENVIRONMENTAL_BUDGET_EXHAUSTED"
    LAUNCHER_BUDGET_EXHAUSTED = "LAUNCHER_BUDGET_EXHAUSTED"
    CREDENTIAL_REFUSED = "CREDENTIAL_REFUSED"
    #: A launcher refusal that is deterministic by construction (§16.3 item
    #: 46). It names the refusal rather than the budget, because there was no
    #: budget: `LAUNCHER_BUDGET_EXHAUSTED` on a refusal that could never have
    #: succeeded tells an operator a number ran out and nothing about the
    #: configuration or plan that has to change.
    LAUNCH_REFUSED = "LAUNCH_REFUSED"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    QUIESCENCE_UNPROVEN = "QUIESCENCE_UNPROVEN"
    OUTPUT_IDENTITY_INVALID = "OUTPUT_IDENTITY_INVALID"


#: The three failures that fit no retry class, because re-running a
#: deterministic thing against an unchanged base cannot produce a different
#: answer (§7.5). Without a dedicated reason each of these falls to the
#: ENVIRONMENTAL default, is retried twice, reproduces itself exactly, and
#: then blocks with an infra-flavoured reason for what is a fact about content.
#: Named as a set rather than behind a predicate. An `is_retryable(reason)`
#: helper stood here and had no production caller for as long as it existed:
#: production decides retryability from the `RetryClass` at classification
#: time — `classify` returns a `block_reason` for exactly these three and a
#: `retry_class` for everything else, so by the time a `BlockReason` exists
#: the decision is already made and asking it again is a second representation
#: of one fact (RC1). The tuple stays because §7.5's membership rule is a
#: statement worth naming and testing; the predicate over it does not.
NON_RETRYABLE: Tuple[BlockReason, ...] = (
    BlockReason.GATE_NOT_FALSIFIABLE,
    BlockReason.CODE_NODE_NO_EFFECT,
    BlockReason.PERMISSION_SCOPE_VIOLATION,
)


#: §11.3's tested property, as a table rather than a sentence: every stored
#: reason admits a legal transition out, and — the part that makes the
#: property mean something — every one admits a *repair*, not only `abandon`.
#: While `UPSTREAM_BLOCKED` was stored this was satisfiable vacuously, since
#: abandon exits every blocked state and a cascaded descendant had no other.
_EXITS: Dict[BlockReason, Tuple[Escape, ...]] = {
    # Re-running an agent cannot make a gate falsifiable, and re-running a
    # deterministic command cannot write different paths. The repair is the
    # operator doing the work by hand and supplying the SHA.
    BlockReason.GATE_NOT_FALSIFIABLE: (Escape.SKIP, Escape.ABANDON),
    BlockReason.CODE_NODE_NO_EFFECT: (Escape.SKIP, Escape.ABANDON),
    BlockReason.PERMISSION_SCOPE_VIOLATION: (Escape.SKIP, Escape.ABANDON),
    # K is a ceiling on a spend, not a verdict, so the forced grant is the
    # designed exit — one attempt per invocation, never a raised cap (§7.5).
    BlockReason.SEMANTIC_BUDGET_EXHAUSTED: (
        Escape.RETRY_FORCE, Escape.SKIP, Escape.ABANDON),
    # Same shape as the semantic ceiling, and the same exit: the reviewer's
    # findings are a verdict about content, so a plain retry against unchanged
    # bytes replays the stored FAIL (B10). `retry --force` grants the one extra
    # attempt, which is also B10's missing operator escape — Strav's
    # `EMPTY_RESUBMISSION_GUARD_V1` shipped with no override at all, so a flaky
    # or environmental FAIL stranded the producer until it minted a new SHA.
    BlockReason.REVIEW_BUDGET_EXHAUSTED: (
        Escape.RETRY_FORCE, Escape.SKIP, Escape.ABANDON),
    # Infra faults: a healthier machine genuinely can produce a different
    # answer, so plain retry is the repair.
    BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED: (
        Escape.RETRY, Escape.SKIP, Escape.ABANDON),
    BlockReason.LAUNCHER_BUDGET_EXHAUSTED: (
        Escape.RETRY, Escape.SKIP, Escape.ABANDON),
    BlockReason.CREDENTIAL_REFUSED: (Escape.RETRY, Escape.SKIP, Escape.ABANDON),
    # The refusal is deterministic against an unchanged configuration, not
    # against every configuration: the operator's repair is to supply what the
    # launcher said was missing, and plain retry is then a genuinely different
    # launch. That is why this is not in NON_RETRYABLE beside the three
    # content-level reasons, which no operator action outside the plan repairs.
    BlockReason.LAUNCH_REFUSED: (Escape.RETRY, Escape.SKIP, Escape.ABANDON),
    # A process group whose absence cannot be proven must be repaired before
    # retry; a retry would overlap an owned survivor with a new attempt.
    BlockReason.QUIESCENCE_UNPROVEN: (Escape.RETRY, Escape.SKIP, Escape.ABANDON),
    # A durable SHA that does not name the recorded commit cannot be merged.
    BlockReason.OUTPUT_IDENTITY_INVALID: (Escape.RETRY, Escape.SKIP, Escape.ABANDON),
    # §8.7 — resolution is human, because a conflict means two output sets
    # overlapped in content though their declared globs did not, which is a
    # planning defect that re-prompting papers over.
    BlockReason.MERGE_CONFLICT: (Escape.SKIP, Escape.ABANDON),
}


def exits_for(reason: BlockReason) -> Tuple[Escape, ...]:
    """The legal transitions out of a stored block reason (§11.3)."""
    return _EXITS[reason]


# ── §7.7 results ────────────────────────────────────────────────────────────

class Adjudication(str, Enum):
    """A result is adjudicated solely against the attempt row it names,
    never against the node's current state (§7.7)."""

    ACCEPTED = "ACCEPTED"
    SUPERSEDED = "SUPERSEDED"
    UNKNOWN_ATTEMPT = "UNKNOWN_ATTEMPT"
    SHA_MISMATCH = "SHA_MISMATCH"


@dataclass(frozen=True)
class ResultRecord:
    """One result, with its payload and its adjudication in one row (§7.7).

    The payload is retained in all four outcomes and cannot be omitted: an
    adjudication recorded without its payload is how a correct FAIL carrying
    two real findings disappeared behind a byte-identical journal.
    """

    node_id: str
    attempt_no: int
    subject_sha: str
    payload: Optional[Mapping[str, Any]]
    adjudication: Optional[Adjudication] = None

    def __post_init__(self) -> None:
        if self.payload is None:
            raise ValueError(
                "a result row carries its payload and its adjudication together; "
                "recording one without the other is what §7.7 exists to prevent")

    @property
    def key(self) -> Tuple[str, int, str]:
        """`(node_id, attempt_no, subject_sha)` — what a result binds (§7.7)."""
        return (self.node_id, self.attempt_no, self.subject_sha)


# ── §7.1 the node the scheduler consumes directly ───────────────────────────

class NodeKind(str, Enum):
    """Three kinds, each with its own VERIFIED predicate (§7.3).

    `REVIEW` is a kind at the type level and never an authored one. B11's
    lesson is that reviewing and producing must be separated in the *type*
    system from day one — Strav made `Role.verifier` mean both "independent
    Verdict-only reviewer" and "artifact-producing task", and PR #74's attempt
    to split them found 33 workflows depending on the hybrid and was closed
    rather than merged. A review node therefore has its own kind, its own
    predicate (§7.3's five clauses, enforced along the review path itself and
    sequenced by `code_review.review_attempt`), and its own evidence chain,
    and shares none of an agent node's clauses.

    But it is **derived by the scheduler, one per build-node attempt**, not
    written by a plan author. An authored review node cannot deliver what this
    gate exists for: "no build lane merges unreviewed" is a property of every
    run, and a property that depends on an author remembering to declare it is
    a property the system does not have. The edge also runs backwards through
    the graph — the build node must be VERIFIED before its review can start,
    and a review FAIL sends the build node back to PENDING — which is a
    scheduler loop, not a dependency. `PlanNode` refuses this kind outright.
    """

    AGENT = "agent"
    CODE = "code"
    REVIEW = "review"


@dataclass(frozen=True)
class PlanNode:
    """A plan node, consumed directly — no second authored type, no converter.

    The constructor refuses three shapes that are not runtime errors but
    design violations, because each is a wedge rather than a failure:

    * an agent node without its own gate selector, which is §7.4's measured
      deadlock — an unscoped post-node gate is red for a sibling's absent
      work, so no node verifies, nothing merges, and the run cannot progress;
    * a code node carrying a gate, which is state nothing evaluates, since a
      code node's acceptance is its exit code and clauses 1-3 do not apply;
    * an agent node carrying `expects_changes`, which is a code-node clause
      and on an agent node would be a field nothing reads (§12.3).
    """

    node_id: str
    kind: NodeKind
    depth: int
    needs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()
    specs: Tuple[str, ...] = ()
    gate_command: Tuple[str, ...] = ()
    gate_selector: Optional[str] = None
    #: §10.2's counting threshold for THIS node's gate, carried from the plan.
    #:
    #: It lives on the node because it is a per-gate fact. It used to live
    #: nowhere: `Plan.to_plan_nodes` copied the gate's runner, argv and
    #: selector and dropped its `min_cases`, so the three adjudication sites
    #: read one unset per-run scalar that was always its default of 1. A plan
    #: declaring 70 told its agent 70 and verified it at 1, and
    #: run-4ee9e079 reached ACCEPTED that way.
    #:
    #: The default of 1 is §10.2's floor rather than a fallback: it is what a
    #: gate means when it demands nothing more than one passing case, and
    #: `verification.adjudicate_counts` refuses anything below it.
    gate_min_cases: int = 1
    command: Tuple[str, ...] = ()
    expects_changes: bool = False
    #: §3.6 B9's first field of the reviewer's declared contract: what this
    #: node was asked to do, carried verbatim from the plan.
    #:
    #: It lives on the node because the reviewer is handed a node, not a plan.
    #: It used to live nowhere: `Plan.to_plan_nodes` copied the id, needs,
    #: outputs and gate and dropped `instruction`, so `build_handoff` read it
    #: through a `getattr(node, "instruction", "")` that could only ever
    #: answer `""`. Every agent node in every run therefore reached its
    #: reviewer with a goal derived from its own gate — "make this command
    #: pass" — and a reviewer that cannot see the contract judges the diff it
    #: was given against the only standard it has, which is not the standard
    #: the plan set.
    #:
    #: The empty default belongs to a code node and to no other kind: a code
    #: node's goal is its command (§6.2), and `AgentNode.instruction` is
    #: `min_length=1`, so a blank one on an agent node is never a plan the
    #: author wrote — it is a projection that dropped the field. Both halves
    #: are refused below rather than defaulted around.
    instruction: str = ""
    #: What this node is authorised to do about each act its plan forbids.
    #:
    #: The reviewer's contract answered *where* work could happen — declared
    #: outputs, the gate, `reads`, `needs` — and nothing answered *what the
    #: code inside may do*. Against the attempt-3 prompt from
    #: run-0120c32064d144c2aa55c344087e0b0a, whose whole brief was "make the
    #: gate pass over selector …, changing only the declared outputs", an
    #: executing object materializer was compliant: the words "pure
    #: derivation", "object mutation", and "injected clients" appeared zero
    #: times, and the reviewers that found real defects did so because that
    #: was the only work available to them.
    #:
    #: Typed rather than prose, and a closed enum over a closed enum. Handing
    #: the reviewer the requirement's own text instead was considered and
    #: declined: the text of the node this exists for says both "pure
    #: derivation and policy module" and "server-side copy it", which puts the
    #: reviewer in the builder's position adjudicating a contradiction, and a
    #: verdict turning on which clause a model weighted is §1.2's prose
    #: deciding a transition by the back door. Admission removes the
    #: contradiction; this carries what survives it.
    #:
    #: The element type is the plan's own `NodeEffect`, which cannot be named
    #: here: `plan_model` imports this module, so naming it would close the
    #: cycle. The projection carries those objects verbatim rather than
    #: re-encoding them, so there is one representation and
    #: `_assert_projection_is_total` compares them by value.
    effects: Tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.node_id).strip():
            raise ValueError("a node needs a non-empty id; identity is not optional")
        if self.depth < 0:
            raise ValueError(f"{self.node_id}: depth is a graph fact, never negative")
        if self.node_id in self.needs:
            raise ValueError(f"{self.node_id}: a node cannot depend on itself")

        if self.kind is NodeKind.REVIEW:
            raise ValueError(
                f"{self.node_id}: a review node is derived by the scheduler from a "
                "build node's verified attempt, never authored in a plan (§7.3). An "
                "authored one would make 'every merged lane was reviewed' depend on "
                "the author remembering to write it")

        if self.kind is NodeKind.AGENT:
            if not self.gate_command:
                raise ValueError(
                    f"{self.node_id}: an agent node's VERIFIED predicate is its own "
                    "gate run twice (§7.4); without a command there is nothing to run")
            if not (self.gate_selector or "").strip():
                raise ValueError(
                    f"{self.node_id}: an agent node needs its own gate selector "
                    "(§7.4). A whole-suite post-node gate is red for a sibling's "
                    "unmerged work, so no node could verify and nothing could merge")
            if self.command:
                raise ValueError(
                    f"{self.node_id}: an agent node's work is the agent's, not a "
                    "command; a command here would be a second execution path")
            if self.expects_changes:
                raise ValueError(
                    f"{self.node_id}: expects_changes is a code-node clause (§7.3); "
                    "on an agent node it is a field nothing reads")
            if self.gate_min_cases < 1:
                raise ValueError(
                    f"{self.node_id}: §10.2 counts `passed >= min_cases >= 1`; a "
                    "gate demanding zero passing cases is green on an empty run")
            if not self.instruction.strip():
                raise ValueError(
                    f"{self.node_id}: an agent node carries the instruction the "
                    "plan declared for it (§3.6 B9). `AgentNode.instruction` is "
                    "min_length=1, so a blank one here is not a plan that omitted "
                    "a goal -- it is a projection that dropped one, and the "
                    "reviewer would be handed a goal derived from the very gate "
                    "it is meant to judge independently")
        else:
            if self.instruction:
                raise ValueError(
                    f"{self.node_id}: a code node's goal is its command (§6.2); an "
                    "instruction here is a field nothing reads (§12.3)")
            if not self.command:
                raise ValueError(
                    f"{self.node_id}: a code node's acceptance is its command's exit "
                    "code (§6.2); without a command there is nothing to accept")
            if self.gate_command or self.gate_selector:
                raise ValueError(
                    f"{self.node_id}: a code node has no gate and no min_cases "
                    "(§7.3); a gate here is state nothing evaluates")
            if self.gate_min_cases != 1:
                raise ValueError(
                    f"{self.node_id}: a code node has no gate to count cases "
                    "for (§7.3); a threshold here is a number nothing reads")

    def to_record(self, state: NodeState) -> "wt.NodeRecord":
        """Project onto the merge protocol's record (§7.1, §8.5).

        A projection, not a conversion: every field is copied unchanged, so
        re-projecting at the same digest and diffing the rows makes a bug in
        this function observable rather than silent.
        """
        return wt.NodeRecord(node_id=self.node_id, depth=self.depth,
                             needs=tuple(self.needs), state=NodeState(state).value,
                             specs=tuple(self.specs))


# ── §11.2 configuration, and the bound preflight enforces ───────────────────

class LivenessBoundUnsatisfied(ValueError):
    """`T` does not exceed the greatest run-window timeout (§9.5, §11.2).

    Refused at construction rather than warned about, because a scheduler
    below the bound does not degrade — below the node timeout it kills healthy
    nodes inside their silent working gap, and below the final-acceptance
    timeout it kills healthy acceptance, which resume then re-runs against the
    same quiescent shape: `STUCK` at the same minute of every resume.
    """


@dataclass(frozen=True)
class SchedulerConfig:
    """Configuration, never plan content — the same reason retry budgets are.

    The finalization timeout (§6.5) is deliberately not a field: no run exists
    at plan time, so it takes no part in the bound below.
    """

    concurrency: int
    node_timeout_s: float
    turn_timeout_s: float
    final_acceptance_timeout_s: float
    backstop_t_s: float
    semantic_ceiling: int
    #: §7.5's ceiling applied to code review, counted separately and never
    #: merged with `semantic_ceiling`. They bound different things: the
    #: semantic ceiling bounds attempts whose *gate* went red, this one bounds
    #: attempts whose gate went green and whose *diff* a reviewer rejected. One
    #: counter would let a node with two gate failures merge unreviewed on its
    #: third try because the shared budget was already spent.
    review_ceiling: int = 3
    environmental_retries: int = 2
    launcher_retries: int = 2
    credential_retries: int = 0

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency is also the pane limit (§7.2); it is ≥ 1")
        if self.semantic_ceiling < 1:
            raise ValueError(
                "a semantic ceiling of zero blocks every agent node on its first "
                "semantic failure, which is a different design, not a setting")
        if self.review_ceiling < 1:
            raise ValueError(
                "a review ceiling of zero blocks every node on its first review "
                "rejection, which is a different design, not a setting")
        for name in ("node_timeout_s", "turn_timeout_s",
                     "final_acceptance_timeout_s", "backstop_t_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} is a wall clock; it is positive")
        for name in ("environmental_retries", "launcher_retries",
                     "credential_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} is a count; it is not negative")
        if self.backstop_t_s <= self.greatest_run_window_s:
            raise LivenessBoundUnsatisfied(
                "LIVENESS_BOUND_UNSATISFIED: the run-level backstop must exceed "
                "the greatest run-window timeout, or it fires inside a healthy "
                f"window. T={self.backstop_t_s}, node_timeout={self.node_timeout_s}, "
                f"final_acceptance_timeout={self.final_acceptance_timeout_s}")

    @property
    def greatest_run_window_s(self) -> float:
        """The greater of the two run windows (§11.2). The finalization
        window has no run row, so it is not one of them."""
        return max(self.node_timeout_s, self.final_acceptance_timeout_s)

    @property
    def pane_limit(self) -> int:
        """§7.2 — the concurrency limit is also the pane limit, because one
        in-flight node holds at most one launch."""
        return self.concurrency

    def retry_budget(self, retry_class: RetryClass) -> int:
        """The non-semantic budgets. SEMANTIC is absent deliberately: its
        budget is scoped to `(node_id, base_sha)` with a cumulative ceiling
        over `(run_id, node_id)`, which is a query over attempt rows rather
        than a constant (§7.5)."""
        if retry_class is RetryClass.ENVIRONMENTAL:
            return self.environmental_retries
        if retry_class is RetryClass.LAUNCHER_TRANSIENT:
            return self.launcher_retries
        raise ValueError(
            "the semantic budget is a COUNT(*) over attempt rows scoped to "
            "(node_id, base_sha) under a (run_id, node_id) ceiling, not a constant")


# ── lifecycle rows the three pieces share ───────────────────────────────────

@dataclass
class NodeLifecycle:
    """A node's authority-tier row — the only tier read at runtime (§5.3).

    `granted_extra_attempts` is the one column §7.5's forced retry costs. It
    exists because the grant must be readable by the scheduler's pre-launch
    guard, and the escape's durable operator-attributed transition lives in
    the audit tier, which §5.3 forbids reading at runtime. An earlier draft
    claimed the ceiling introduced no new state; that was false the moment
    `retry --force` existed.
    """

    node_id: str
    state: NodeState = NodeState.PENDING
    attempt_no: int = 0
    block_reason: Optional[BlockReason] = None
    output_sha: Optional[str] = None
    granted_extra_attempts: int = 0

    def __post_init__(self) -> None:
        if (self.block_reason is not None) != (self.state is NodeState.BLOCKED):
            raise ValueError(
                f"{self.node_id}: a block reason and the BLOCKED state are one "
                "fact; storing either without the other is a row that cannot be "
                "reported and an exit that cannot be looked up (§11.3)")


@dataclass
class AttemptRecord:
    """One attempt of one node. The window §7.6 watches opens with this row.

    `launched_at` is `None` until the adapter reports the agent launched, and
    that is what arms the process-alive and turn-count signals. Before it, the
    two are undefined by construction rather than by omission: the attempt
    window covers worktree creation, provision, the pre-gate, and the baseline
    inventory, none of which have a process or a transcript yet.
    """

    run_id: str
    node_id: str
    attempt_no: int
    base_sha: str
    state: NodeState = NodeState.RUNNING
    started_at: float = 0.0
    launched_at: Optional[float] = None
    pid: Optional[int] = None
    turn_count: int = 0
    retry_class: Optional[RetryClass] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def armed(self) -> bool:
        """Whether §7.6's first two signals apply yet."""
        return self.launched_at is not None

    @property
    def key(self) -> Tuple[str, str, int]:
        return (self.run_id, self.node_id, self.attempt_no)

    @property
    def guidance_key(self) -> Tuple[str, str]:
        """The scope retry guidance is valid within: `(node_id, base_sha)`.

        §7.5 already scopes the prompt-mutation budget this way, and for the
        same reason the guidance itself must be: a review finding or a
        verification failure is evidence *about a tree*. When an upstream merge
        advances the integration head, the next attempt starts from a base at
        which that tree no longer exists, and handing the agent findings
        derived from it is instructing it to fix code that is not there. Keyed
        on `node_id` alone the ledger had no expiry at all — nothing cleared
        it when the base moved, because nothing knew the base had moved.

        A tuple rather than a cleared counter, for the reason §7.5 gives about
        the budget itself: the scope is derived from a stored fact the attempt
        row already carries, so there is no reset event to fire and nothing
        that can drift.
        """
        return (self.node_id, self.base_sha)
