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
    """The durable node states.

    ``ACCEPTED`` belongs only to a derived review node: it is terminal review
    evidence and deliberately does not mean that a source-producing node was
    merged. Build lanes still use ``VERIFIED`` then ``MERGED``.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VERIFIED = "VERIFIED"
    ACCEPTED = "ACCEPTED"
    MERGED = "MERGED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class LanePhase(str, Enum):
    """The persistent build/review loop phase for one reviewable lane."""

    BUILDING = "BUILDING"
    CANDIDATE_READY = "CANDIDATE_READY"
    REVIEWING = "REVIEWING"
    REPAIR_HANDOFF = "REPAIR_HANDOFF"
    REPAIRING = "REPAIRING"
    WAITING_FOR_NEW_CANDIDATE = "WAITING_FOR_NEW_CANDIDATE"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


LANE_PHASE_TERMINAL: Tuple[LanePhase, ...] = (
    LanePhase.ACCEPTED,
    LanePhase.BLOCKED,
    LanePhase.CANCELLED,
)


class CandidateReviewState(str, Enum):
    """A reviewer dispatch is published before prompt submission, then final."""

    PUBLISHED = "PUBLISHED"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"


class ReviewVerdict(str, Enum):
    PASS = "PASS"
    REJECTED = "REJECTED"


class RepairHandoffState(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"


#: Nothing transitions out of these, ever (§7.3).
ABSOLUTELY_TERMINAL: Tuple[NodeState, ...] = (
    NodeState.MERGED,
    NodeState.ACCEPTED,
    NodeState.CANCELLED,
)

#: Operator action may unblock these; they are stopped, not immutable.
OPERATOR_TERMINAL: Tuple[NodeState, ...] = (NodeState.BLOCKED,)

#: States that stop a node without merging source output. Shared with the
#: merge frontier so cascade semantics cannot drift from lifecycle semantics.
TERMINAL_WITHOUT_MERGE: Tuple[NodeState, ...] = (NodeState.BLOCKED, NodeState.CANCELLED)


class LaneRetryClass(str, Enum):
    """Durable correction-loop budget classes, separate from attempts.

    `TEST_REVIEW_REJECTION` is its own class rather than a use of
    `REVIEW_REJECTION`, because the two loops converge differently and
    sharing one budget makes the shorter one govern both. An implementation
    review rejects a diff whose gate already passes; a test review rejects a
    candidate whose whole purpose is to be able to fail, and the corrections
    it asks for -- a missing negative case, a fixture that is not real, an
    assertion on plumbing -- are rewrites rather than repairs. A tester that
    spends the implementation lane's budget arriving at a strong test suite
    leaves nothing for the implementation it was written to gate.
    """

    SEMANTIC = "SEMANTIC"
    ENVIRONMENTAL = "ENVIRONMENTAL"
    LAUNCHER_TRANSIENT = "LAUNCHER_TRANSIENT"
    REVIEW_REJECTION = "REVIEW_REJECTION"
    TEST_REVIEW_REJECTION = "TEST_REVIEW_REJECTION"


class TestStrengthContract(str, Enum):
    """Which test-acceptance rules a run was **created** under.

    A run is pinned to this at creation and it is never rewritten. That is the
    whole rollout invariant: a run created before executable gate-strength
    existed stays reproducible under the contract it was created with, and a
    run created after it cannot escape into the older one.

    ``LEGACY`` is what a NULL ledger column reads as — a run created when a
    tests node was accepted on "new cases, red at the parent". Its tests nodes
    are classified (`LEGACY_TEST_STRENGTH_UNPROVEN`) and reported, never
    retroactively reopened, invalidated, or rerun.

    ``STRENGTH_V1`` is the contract this runtime creates runs under: an
    independently reviewed test candidate, measured coverage, an executed
    negative control, and an implementation bound to the exact accepted test
    bytes.
    """

    LEGACY = "LEGACY"
    STRENGTH_V1 = "test-strength.v1"


#: The contract a run is pinned to when its ledger records none. Named rather
#: than spelled `LEGACY` at each reader, because "NULL means legacy" is one
#: decision and three copies of it would be three places to disagree.
DEFAULT_TEST_STRENGTH_CONTRACT = TestStrengthContract.LEGACY


class TestStrengthPhase(str, Enum):
    """What an operator surface calls a node under the strength contract.

    A projection of `(kind, NodeState, LanePhase, accepted?, paired?)`, not a
    fourth state machine. Nothing transitions on these and nothing stores one:
    a second durable lifecycle beside `NodeState` and `LanePhase` would be two
    representations of one fact, which is the shape this design convicts
    everywhere else.

    It exists because the surfaces were lying by omission. A tests node whose
    candidate had been authored and committed rendered as `tester MERGED`,
    which reads as "these tests are done and good" — and in
    run-8d1a71f463e4430f92a125a8f8b3731d it meant "these bytes reached the
    integration branch", nothing more. The distinction between *private
    acceptance* and *paired merge* is the one an operator most needs and the
    one the old vocabulary could not express.
    """

    TEST_BUILDING = "TEST_BUILDING"
    TEST_CANDIDATE_READY = "TEST_CANDIDATE_READY"
    TEST_REVIEWING = "TEST_REVIEWING"
    TEST_REJECTED = "TEST_REJECTED"
    TEST_REPAIRING = "TEST_REPAIRING"
    TEST_ACCEPTED = "TEST_ACCEPTED"
    TEST_BLOCKED = "TEST_BLOCKED"
    #: Integrated, and with no evidence attributable to the exact candidate
    #: its state rests on. The rollout's classification, rendered: the node
    #: stays terminal and its dependants stay admitted, and an operator
    #: reading the run is told what is unproven rather than shown
    #: `TEST_BUILDING`, which is false, or `TEST_ACCEPTED`, which is worse.
    TEST_LEGACY_UNPROVEN = "TEST_LEGACY_UNPROVEN"
    IMPLEMENTATION_PENDING = "IMPLEMENTATION_PENDING"
    IMPLEMENTATION_BUILDING = "IMPLEMENTATION_BUILDING"
    IMPLEMENTATION_REVIEWING = "IMPLEMENTATION_REVIEWING"
    IMPLEMENTATION_ACCEPTED = "IMPLEMENTATION_ACCEPTED"
    PAIRED_MERGED = "PAIRED_MERGED"


#: Where a tests node's bytes are, which `MERGED` alone cannot say.
class TestBytesLocation(str, Enum):
    PRIVATE = "private"        # committed to the attempt's candidate ref only
    STAGED = "staged"          # accepted, not yet on the integration branch
    INTEGRATED = "integrated"  # merged into the integration branch


def test_strength_phase(
    kind: "NodeKind",
    state: NodeState,
    lane_phase: Optional[LanePhase],
    *,
    accepted: bool = False,
    paired: bool = False,
) -> Optional[TestStrengthPhase]:
    """Name this node's position in the test-strength lifecycle.

    `None` for a kind the lifecycle does not describe — a code node, a derived
    review node — so a surface renders nothing rather than an invented phase.
    """
    if kind is NodeKind.TESTS:
        if state is NodeState.BLOCKED:
            return TestStrengthPhase.TEST_BLOCKED
        if accepted:
            # TEST_ACCEPTED whether or not the bytes reached the integration
            # branch, because acceptance and integration are different facts
            # and the surface reports the second one separately
            # (`TestBytesLocation`). Collapsing them is what made a private
            # acceptance render as `tester MERGED`.
            return TestStrengthPhase.TEST_ACCEPTED
        if state in (NodeState.MERGED, NodeState.ACCEPTED):
            return TestStrengthPhase.TEST_LEGACY_UNPROVEN
        if lane_phase is LanePhase.CANDIDATE_READY:
            return TestStrengthPhase.TEST_CANDIDATE_READY
        if lane_phase is LanePhase.REVIEWING:
            return TestStrengthPhase.TEST_REVIEWING
        if lane_phase is LanePhase.REPAIR_HANDOFF:
            return TestStrengthPhase.TEST_REJECTED
        if lane_phase in (LanePhase.REPAIRING,
                          LanePhase.WAITING_FOR_NEW_CANDIDATE):
            return TestStrengthPhase.TEST_REPAIRING
        return TestStrengthPhase.TEST_BUILDING
    if kind is NodeKind.AGENT:
        if state is NodeState.MERGED:
            return (TestStrengthPhase.PAIRED_MERGED if paired
                    else TestStrengthPhase.IMPLEMENTATION_ACCEPTED)
        if lane_phase is LanePhase.ACCEPTED:
            return TestStrengthPhase.IMPLEMENTATION_ACCEPTED
        if lane_phase in (LanePhase.REVIEWING, LanePhase.CANDIDATE_READY,
                          LanePhase.REPAIR_HANDOFF):
            return TestStrengthPhase.IMPLEMENTATION_REVIEWING
        if state is NodeState.PENDING:
            return TestStrengthPhase.IMPLEMENTATION_PENDING
        return TestStrengthPhase.IMPLEMENTATION_BUILDING
    return None


class ActorSessionState(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


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
      wanted to pause and had no verb that said so. `run pause` is that verb
      now, and it writes nothing at all: a pause is not a lifecycle
      transition, so a run stopped that way has no cause because it has no
      declared outcome to attach one to.
    * `DISCARDED` — the operator ran `run cancel --discard`, which is the
      destructive verb: it exists to end a run for good, and a resume that
      reopened it would defeat the only thing it does. It is a separate
      member rather than a reuse of `ABANDONED` because the two are different
      facts — `ABANDONED` says every node was individually adjudicated as
      work the run should finish without, and `DISCARDED` says the operator
      threw the run away without adjudicating anything. §1.2 wants that
      distinction structural, in the stored value, not inferred from which
      verb an operator is remembered to have typed. Adding a member is
      additive: a receipt or ledger written before it exists carries one of
      the two older values and parses unchanged, unlike a field made
      unconditionally required after the fact (§19 M21).
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
    DISCARDED = "DISCARDED"
    ABANDONED = "ABANDONED"


#: The causes a `CANCELLED` may be reopened from (§7.3, §7.8). `MERGED`, an
#: `ABANDONED` `CANCELLED`, and a `DISCARDED` one remain absolutely terminal;
#: a `RUN_CANCEL` `CANCELLED` is terminal only because the operator asked the
#: machine to stop, and a resume of that same run is the operator withdrawing
#: the request. `DISCARDED` is not withdrawable in that sense: the operator
#: asked for the run to end, not to stop, and `run pause` is the verb for the
#: other intent. Membership is data rather than a branch so that the guard,
#: the resume predicate, and the tests read one list.
REOPENABLE_CANCEL_CAUSES: Tuple[CancelCause, ...] = (CancelCause.RUN_CANCEL,)


class MergeCause(str, Enum):
    """How a node reached `MERGED` — the same shape as `CancelCause`, one
    column over, and forced by the same defect one state along.

    `MERGED` was one word carrying two facts. A node reaches it down one of
    two paths, and §1.1 item 4 is the whole difference between them:

    * `SCHEDULER` — the run merged it. The node passed §7.3's four-clause
      verification predicate, went `VERIFIED`, and was merged by §8.5's
      deterministic frontier with the ancestry proof §8.6 requires. The
      complete evidence chain scoped to its kind exists and is enumerable
      from the ledger: attempt row, output commit, gate results, the
      permission check over the measured delta, and — for an agent node —
      a reviewer that passed the diff before `mark_verified` was called.
    * `OPERATOR_ACCEPTED` — `maestro skip` (§11.3). The operator supplied the
      work by hand and asserted it. The five identity gates `skip` runs are
      real and are not relaxed by this member: the accepted SHA is still a
      canonical digest, still descends from the latest attempt base, is still
      an ancestor of HEAD, still *equals* HEAD, and the tree is still clean.
      What it does not carry is the rest of the chain — no reviewer verdict
      is required, no post-node gate need have passed, no permission check
      over a measured delta was taken, and there is no merge commit. Those
      are the facts §1.1 item 4 enumerates, and a node merged this way holds
      none of them.

    Stored typed on `node_lifecycle.merge_cause`, because §1.2 forbids a
    reader reconstructing a lifecycle fact from prose — and reconstructing it
    was the only thing an operator could do. In git the difference is visible:
    a merged lane leaves a merge commit and a skipped lane leaves only the
    attempt commit, so reading the integration log was the sole way to tell
    the two apart. `run status` reported both as `MERGED` with an
    `output_sha`, identically, over a node whose every attempt was `CANCELLED`
    or `BLOCKED` and none of which carried a verdict (#93).

    The absent evidence itself is *not* a second member here. What the chain
    holds is a set of facts about a node's history, and encoding a summary of
    them in this vocabulary would be one fact in two representations — the RC1
    shape §4 convicts. The cause says who wrote the state; the skip
    transition's typed detail says what the ledger could and could not show
    about the node at the moment it was written.

    Adding a member later stays additive, as it did for `CancelCause`: a
    ledger written before this column carries neither value and reads
    `UNRECORDED` (below), never one of these two.
    """

    SCHEDULER = "SCHEDULER"
    OPERATOR_ACCEPTED = "OPERATOR_ACCEPTED"


#: The merge causes that carry no evidence chain of their own (§1.1 item 4),
#: so an audit looking for merges whose evidence must be established some
#: other way reads this list rather than naming the member. Data rather than
#: a branch, exactly as `REOPENABLE_CANCEL_CAUSES` is.
UNEVIDENCED_MERGE_CAUSES: Tuple[MergeCause, ...] = (MergeCause.OPERATOR_ACCEPTED,)

#: What a reader says about a `MERGED` node whose cause was never recorded:
#: a row written before the column existed. It is deliberately **not** a
#: `MergeCause` member, because it is the absence of the fact rather than a
#: third value of it, and nothing may ever write it. The migration invents no
#: facts (§7.3's rule for an unrecorded `cancel_cause`): reading an
#: unrecorded merge as `SCHEDULER` would have every pre-existing row assert
#: an evidence chain nobody checked, which is the exact false confidence this
#: column exists to remove.
MERGE_CAUSE_UNRECORDED = "UNRECORDED"


def merge_cause_label(
    state: NodeState, merge_cause: Optional[MergeCause]
) -> Optional[str]:
    """The one derivation of "how did this node reach MERGED".

    Three answers and a fourth non-answer: the two `MergeCause` members,
    `UNRECORDED` for a `MERGED` row older than the column, and `None` for a
    node that is not `MERGED` at all — where the question does not arise and
    the column is NULL for that reason instead. `run status`, the visualizer,
    and the tests read this function rather than each re-deriving the pair,
    so the three cannot drift into three answers (RC1).
    """
    if state is not NodeState.MERGED:
        return None
    return merge_cause.value if merge_cause else MERGE_CAUSE_UNRECORDED


class PendingCause(str, Enum):
    """How a node reached `PENDING` after leaving it — the same shape as
    `CancelCause` and `MergeCause`, one column over.

    `PENDING` was one word carrying three facts. A node reaches it down one
    of three paths after it has already left the frontier, and a reader of
    `node_lifecycle` could not tell which (#103):

    * `SCHEDULER` — `fail_attempt`, or the resume path that closes an
      inherited RUNNING attempt. The machine put the node back on the
      frontier. Nothing about the operator is in this write.
    * `OPERATOR_RETRY` — `retry` / `retry --force` / `retry --grant`. The
      operator handed the node back. A grant's magnitude stays on
      `granted_extra_attempts`; this member is the identity of the writer,
      so a plain retry (delta 0) is no longer indistinguishable from a
      scheduler write. The grant column is not reused as a proxy for who.
    * `OPERATOR_RESUME` — `_reopen_run_cancelled_node`, the resume half of
      `run cancel`. The operator withdrew a stop request. Nothing was
      adjudicated about the node, which is why the reopen exists; the cause
      still has to say so, because PENDING itself does not.

    Seeded PENDING — `create_run`'s initial row — never left the frontier,
    so the question does not arise and the column stays NULL. NULL on a
    PENDING row written before this column is the same absence, never
    `SCHEDULER`: the migration invents no facts.

    Stored typed on `node_lifecycle.pending_cause`, because §1.2 forbids a
    reader reconstructing a lifecycle fact from transition prose.
    """

    SCHEDULER = "SCHEDULER"
    OPERATOR_RETRY = "OPERATOR_RETRY"
    OPERATOR_RESUME = "OPERATOR_RESUME"


def pending_cause_label(
    state: NodeState, pending_cause: Optional[PendingCause]
) -> Optional[str]:
    """The one derivation of "who put this node on the frontier".

    Four answers: the three `PendingCause` members, and `None` both for a
    node that is not `PENDING` and for a `PENDING` row that never left the
    frontier (or whose cause was never recorded). `run status` and the
    tests read this function rather than each re-deriving the pair (RC1).
    A reader must not guess `SCHEDULER` from a NULL.
    """
    if state is not NodeState.PENDING:
        return None
    return pending_cause.value if pending_cause else None


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
    #: A declared output exists on disk but git will not commit it — gitignored,
    #: excluded, or otherwise outside inventory's universe. Re-running the
    #: agent cannot make git carry the path; the declaration or the ignore
    #: rule has to change.
    DECLARED_OUTPUT_UNCOMMITTABLE = "DECLARED_OUTPUT_UNCOMMITTABLE"
    #: A code node's command defined module-level symbols that nothing on the
    #: merged surface references (#118). `min_cases` is a floor with no
    #: ceiling, so before this an attempt could ship arbitrary unreachable
    #: machinery and stay green. Re-running a deterministic command against an
    #: unchanged base emits the same symbols, so the repair is the command or
    #: the plan, never another attempt.
    PRODUCED_SYMBOL_UNREFERENCED = "PRODUCED_SYMBOL_UNREFERENCED"


#: The failures that fit no retry class, because re-running a
#: deterministic thing against an unchanged base cannot produce a different
#: answer (§7.5). Without a dedicated reason each of these falls to the
#: ENVIRONMENTAL default, is retried twice, reproduces itself exactly, and
#: then blocks with an infra-flavoured reason for what is a fact about content.
#: Named as a set rather than behind a predicate. An `is_retryable(reason)`
#: helper stood here and had no production caller for as long as it existed:
#: production decides retryability from the `RetryClass` at classification
#: time — `classify` returns a `block_reason` for exactly these and a
#: `retry_class` for everything else, so by the time a `BlockReason` exists
#: the decision is already made and asking it again is a second representation
#: of one fact (RC1). The tuple stays because §7.5's membership rule is a
#: statement worth naming and testing; the predicate over it does not.
NON_RETRYABLE: Tuple[BlockReason, ...] = (
    BlockReason.GATE_NOT_FALSIFIABLE,
    BlockReason.CODE_NODE_NO_EFFECT,
    BlockReason.PERMISSION_SCOPE_VIOLATION,
    BlockReason.DECLARED_OUTPUT_UNCOMMITTABLE,
    BlockReason.PRODUCED_SYMBOL_UNREFERENCED,
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
    BlockReason.DECLARED_OUTPUT_UNCOMMITTABLE: (Escape.SKIP, Escape.ABANDON),
    BlockReason.PRODUCED_SYMBOL_UNREFERENCED: (Escape.SKIP, Escape.ABANDON),
    # K is a ceiling on a spend, not a verdict, so the forced grant is the
    # designed exit — one attempt per invocation, never a raised cap (§7.5).
    BlockReason.SEMANTIC_BUDGET_EXHAUSTED: (
        Escape.RETRY_FORCE,
        Escape.SKIP,
        Escape.ABANDON,
    ),
    # Same shape as the semantic ceiling, and the same exit: the reviewer's
    # findings are a verdict about content, so a plain retry against unchanged
    # bytes replays the stored FAIL (B10). `retry --force` grants the one extra
    # attempt, which is also B10's missing operator escape — Strav's
    # `EMPTY_RESUBMISSION_GUARD_V1` shipped with no override at all, so a flaky
    # or environmental FAIL stranded the producer until it minted a new SHA.
    BlockReason.REVIEW_BUDGET_EXHAUSTED: (
        Escape.RETRY_FORCE,
        Escape.SKIP,
        Escape.ABANDON,
    ),
    # Infra faults: a healthier machine genuinely can produce a different
    # answer, so plain retry is the repair.
    BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED: (
        Escape.RETRY,
        Escape.SKIP,
        Escape.ABANDON,
    ),
    BlockReason.LAUNCHER_BUDGET_EXHAUSTED: (Escape.RETRY, Escape.SKIP, Escape.ABANDON),
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
                "recording one without the other is what §7.7 exists to prevent"
            )

    @property
    def key(self) -> Tuple[str, int, str]:
        """`(node_id, attempt_no, subject_sha)` — what a result binds (§7.7)."""
        return (self.node_id, self.attempt_no, self.subject_sha)


# ── §7.1 the node the scheduler consumes directly ───────────────────────────


class NodeKind(str, Enum):
    """Four kinds, each with its own VERIFIED predicate (§7.3, §1.1 item 4).

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

    `TESTS` is authored. It writes the test files a later `AGENT` (build)
    node must make pass, and it carries its own evidence chain: test files
    only, at least one new collected case, each new case red at the parent
    commit. Reusing the agent chain here is how hollow tests shipped.
    """

    AGENT = "agent"
    CODE = "code"
    REVIEW = "review"
    TESTS = "tests"


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
    #: The tests node's authored test-strength contract, or `None`.
    #:
    #: `None` is the v3 shape and only the v3 shape. It is not a default this
    #: runtime works around: `maestro run start` refuses to *create* a run
    #: whose tests nodes carry none, and the run row records which contract
    #: the run was created under, so a v3 run resumes under v3 rules while a
    #: new run cannot be started under them (`TEST_STRENGTH_CONTRACT_ABSENT`).
    #:
    #: Typed as `Any` for the reason `effects` is: the concrete type is
    #: `plan_model.TestStrength`, `plan_model` imports this module, and naming
    #: it here would close the cycle. The projection carries the object
    #: verbatim so `_assert_projection_is_total` compares it by value.
    test_strength: Optional[Any] = None

    def __post_init__(self) -> None:
        if not str(self.node_id).strip():
            raise ValueError("a node needs a non-empty id; identity is not optional")
        if self.depth < 0:
            raise ValueError(f"{self.node_id}: depth is a graph fact, never negative")
        if self.node_id in self.needs:
            raise ValueError(f"{self.node_id}: a node cannot depend on itself")

        if self.test_strength is not None and self.kind is not NodeKind.TESTS:
            raise ValueError(
                f"{self.node_id}: a test-strength contract belongs to a tests "
                "node; on any other kind it is a field nothing reads (§12.3)"
            )

        if self.kind is NodeKind.REVIEW:
            raise ValueError(
                f"{self.node_id}: a review node is derived by the scheduler from a "
                "build node's verified attempt, never authored in a plan (§7.3). An "
                "authored one would make 'every merged lane was reviewed' depend on "
                "the author remembering to write it"
            )

        if self.kind is NodeKind.AGENT:
            if not self.gate_command:
                raise ValueError(
                    f"{self.node_id}: an agent node's VERIFIED predicate is its own "
                    "gate run twice (§7.4); without a command there is nothing to run"
                )
            if not (self.gate_selector or "").strip():
                raise ValueError(
                    f"{self.node_id}: an agent node needs its own gate selector "
                    "(§7.4). A whole-suite post-node gate is red for a sibling's "
                    "unmerged work, so no node could verify and nothing could merge"
                )
            if self.command:
                raise ValueError(
                    f"{self.node_id}: an agent node's work is the agent's, not a "
                    "command; a command here would be a second execution path"
                )
            if self.expects_changes:
                raise ValueError(
                    f"{self.node_id}: expects_changes is a code-node clause (§7.3); "
                    "on an agent node it is a field nothing reads"
                )
            if self.gate_min_cases < 1:
                raise ValueError(
                    f"{self.node_id}: §10.2 counts `passed >= min_cases >= 1`; a "
                    "gate demanding zero passing cases is green on an empty run"
                )
            if not self.instruction.strip():
                raise ValueError(
                    f"{self.node_id}: an agent node carries the instruction the "
                    "plan declared for it (§3.6 B9). `AgentNode.instruction` is "
                    "min_length=1, so a blank one here is not a plan that omitted "
                    "a goal -- it is a projection that dropped one, and the "
                    "reviewer would be handed a goal derived from the very gate "
                    "it is meant to judge independently"
                )
        elif self.kind is NodeKind.TESTS:
            if not self.gate_command:
                raise ValueError(
                    f"{self.node_id}: a tests node's evidence chain counts "
                    "cases from its gate; without a command there is nothing "
                    "to collect"
                )
            if not (self.gate_selector or "").strip():
                raise ValueError(
                    f"{self.node_id}: a tests node needs its own gate selector "
                    "so new cases can be counted against that selector"
                )
            if self.command:
                raise ValueError(
                    f"{self.node_id}: a tests node's work is the agent's, not a "
                    "command; a command here would be a second execution path"
                )
            if self.expects_changes:
                raise ValueError(
                    f"{self.node_id}: expects_changes is a code-node clause "
                    "(§7.3); on a tests node it is a field nothing reads"
                )
            if self.gate_min_cases < 1:
                raise ValueError(
                    f"{self.node_id}: a tests node counts `new cases >= 1`; a "
                    "gate demanding zero cases cannot refuse a hollow file"
                )
            if not self.instruction.strip():
                raise ValueError(
                    f"{self.node_id}: a tests node carries the instruction the "
                    "plan declared for it (§3.6 B9)"
                )
        else:
            if self.instruction:
                raise ValueError(
                    f"{self.node_id}: a code node's goal is its command (§6.2); an "
                    "instruction here is a field nothing reads (§12.3)"
                )
            if not self.command:
                raise ValueError(
                    f"{self.node_id}: a code node's acceptance is its command's exit "
                    "code (§6.2); without a command there is nothing to accept"
                )
            if self.gate_command or self.gate_selector:
                raise ValueError(
                    f"{self.node_id}: a code node has no gate and no min_cases "
                    "(§7.3); a gate here is state nothing evaluates"
                )
            if self.gate_min_cases != 1:
                raise ValueError(
                    f"{self.node_id}: a code node has no gate to count cases "
                    "for (§7.3); a threshold here is a number nothing reads"
                )

    def to_record(self, state: NodeState) -> "wt.NodeRecord":
        """Project onto the merge protocol's record (§7.1, §8.5).

        A projection, not a conversion: every field is copied unchanged, so
        re-projecting at the same digest and diffing the rows makes a bug in
        this function observable rather than silent.
        """
        return wt.NodeRecord(
            node_id=self.node_id,
            depth=self.depth,
            needs=tuple(self.needs),
            state=NodeState(state).value,
            specs=tuple(self.specs),
        )


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
    #: §7.5's ceiling applied to **test** review, counted separately from
    #: `review_ceiling` for the reason that one is counted separately from
    #: `semantic_ceiling`: they bound different loops. A rejected test
    #: candidate is corrected by writing cases that did not exist, which is a
    #: different and usually longer convergence than repairing a diff whose
    #: gate is already green. Sharing one counter would let a lane whose tests
    #: took three rounds to become strong reach its implementation review with
    #: no budget left, and block a lane that never failed a diff review.
    test_review_ceiling: int = 3
    environmental_retries: int = 2
    launcher_retries: int = 2
    credential_retries: int = 0

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency is also the pane limit (§7.2); it is ≥ 1")
        if self.semantic_ceiling < 1:
            raise ValueError(
                "a semantic ceiling of zero blocks every agent node on its first "
                "semantic failure, which is a different design, not a setting"
            )
        if self.review_ceiling < 1:
            raise ValueError(
                "a review ceiling of zero blocks every node on its first review "
                "rejection, which is a different design, not a setting"
            )
        if self.test_review_ceiling < 1:
            raise ValueError(
                "a test review ceiling of zero blocks every tests node on its "
                "first review rejection, which is a different design, not a "
                "setting"
            )
        for name in (
            "node_timeout_s",
            "turn_timeout_s",
            "final_acceptance_timeout_s",
            "backstop_t_s",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} is a wall clock; it is positive")
        for name in ("environmental_retries", "launcher_retries", "credential_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} is a count; it is not negative")
        if self.backstop_t_s <= self.greatest_run_window_s:
            raise LivenessBoundUnsatisfied(
                "LIVENESS_BOUND_UNSATISFIED: the run-level backstop must exceed "
                "the greatest run-window timeout, or it fires inside a healthy "
                f"window. T={self.backstop_t_s}, node_timeout={self.node_timeout_s}, "
                f"final_acceptance_timeout={self.final_acceptance_timeout_s}"
            )

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
            "(node_id, base_sha) under a (run_id, node_id) ceiling, not a constant"
        )


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
    #: The durable authority that returned a node to the frontier. Seeded
    #: PENDING rows and non-PENDING states carry no cause.
    pending_cause: Optional[PendingCause] = None
    #: The durable build/review-loop authority.  It is absent for ordinary
    #: DAG nodes and for ledgers written before persistent review existed.
    lane_phase: Optional[LanePhase] = None

    def __post_init__(self) -> None:
        if (self.block_reason is not None) != (self.state is NodeState.BLOCKED):
            raise ValueError(
                f"{self.node_id}: a block reason and the BLOCKED state are one "
                "fact; storing either without the other is a row that cannot be "
                "reported and an exit that cannot be looked up (§11.3)"
            )


@dataclass(frozen=True)
class LaneCandidate:
    """One immutable commit the persistent builder published for review."""

    run_id: str
    build_node_id: str
    candidate_seq: int
    candidate_sha: str
    parent_candidate_sha: Optional[str]
    builder_generation: int
    created_at: str


@dataclass(frozen=True)
class CandidateReview:
    """One exactly-once review of one immutable candidate commit."""

    run_id: str
    review_node_id: str
    candidate_sha: str
    reviewer_generation: int
    state: CandidateReviewState
    dispatched_at: Optional[str]
    review_digest: Optional[str]
    receipt_path: Optional[str]
    findings: Tuple[Mapping[str, Any], ...]
    verdict: Optional[ReviewVerdict]
    completed_at: Optional[str]

    @property
    def terminal(self) -> bool:
        return self.state is CandidateReviewState.COMPLETED


@dataclass(frozen=True)
class RepairHandoff:
    """The one builder-targeted repair handoff for a rejected candidate."""

    run_id: str
    build_node_id: str
    rejected_candidate_sha: str
    findings: Tuple[Mapping[str, Any], ...]
    state: RepairHandoffState
    builder_generation: int
    submitted_at: Optional[str]
    acknowledged_at: Optional[str]


@dataclass(frozen=True)
class CandidatePublication:
    """The publication result; ``created=False`` is a deterministic replay."""

    candidate: LaneCandidate
    created: bool


@dataclass(frozen=True)
class ReviewBegin:
    """Whether a caller owns the first dispatch for this candidate review."""

    review: CandidateReview
    created: bool
    should_dispatch: bool


@dataclass(frozen=True)
class ReviewCompletion:
    """The first terminal review result, or a persisted late/duplicate no-op."""

    review: CandidateReview
    completed: bool


@dataclass(frozen=True)
class RejectionHandoff:
    """The atomic review rejection and its optional persisted handoff."""

    review: CandidateReview
    handoff: Optional[RepairHandoff]
    completed: bool
    created: bool


@dataclass(frozen=True)
class HandoffSubmission:
    """Whether the exact persisted handoff was delivered to its builder."""

    handoff: RepairHandoff
    submitted: bool


@dataclass(frozen=True)
class HandoffAcknowledgement:
    """Whether this generation acknowledged the exact rejected candidate."""

    handoff: RepairHandoff
    acknowledged: bool


@dataclass(frozen=True)
class LaneRetrySpend:
    """One correction-loop budget debit, independent of builder attempts."""

    run_id: str
    build_node_id: str
    retry_class: LaneRetryClass
    cycle_seq: int
    candidate_sha: Optional[str]
    detail: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True)
class LaneRetrySpendRecord:
    spend: LaneRetrySpend
    created: bool


@dataclass(frozen=True)
class ActorSession:
    """The durable identity of one persistent builder or reviewer generation."""

    run_id: str
    build_node_id: str
    actor_role: str
    generation: int
    state: ActorSessionState
    pane_id: Optional[str]
    tab_id: Optional[str]
    session_path: Optional[str]
    correlation_token: Optional[str]
    updated_at: str


@dataclass(frozen=True)
class ActorSessionRecovery:
    session: ActorSession
    recovered: bool


#: The `attempts.extra_json` key a repair attempt's own row carries: which
#: rejected attempt it is repairing, which integration head it was nevertheless
#: derived from, and how long the repair chain it ends is.
#:
#: Declared here rather than in `retry_policy` because `AttemptRecord` reads it
#: and this module is read-only from there — the shared vocabulary lives with
#: the row it describes, and `retry_policy` imports the name so the writer and
#: the three readers below cannot drift into two spellings.
REPAIR_KEY = "repair_of"


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
    #: Host whose pid namespace `pid` belongs to, and the start time of
    #: that process. `None` on a ledger written before the columns and
    #: when no pid was recorded. `attempt_liveness` reads both as
    #: unknown, never as dead and never as alive.
    attempt_host: Optional[str] = None
    attempt_start_epoch: Optional[float] = None

    @property
    def armed(self) -> bool:
        """Whether §7.6's first two signals apply yet."""
        return self.launched_at is not None

    @property
    def key(self) -> Tuple[str, str, int]:
        return (self.run_id, self.node_id, self.attempt_no)

    @property
    def integration_head(self) -> str:
        """The integration head this attempt was derived from (§8.1).

        Equal to `base_sha` for every attempt that branched straight off the
        integration head, which was every attempt before repair bases existed
        and is still every attempt that is not repairing a rejected diff. A
        repair attempt branches from the *rejected attempt's output commit*
        instead, so its `base_sha` is no longer the integration head, and the
        head it was nevertheless derived from is recorded on its own row under
        `REPAIR_KEY` when the row is opened.

        Read from the stored marker rather than recomputed, because the fact
        wanted here is which head the attempt was *branched from*, and
        `worktree.integration_head` answers which head exists *now* — the two
        differ exactly when a sibling merged, which is the case every reader of
        this property exists to detect.
        """
        marker = (self.extra or {}).get(REPAIR_KEY)
        if isinstance(marker, dict):
            head = marker.get("integration_head")
            if isinstance(head, str) and head:
                return head
        return self.base_sha

    @property
    def repair_chain_length(self) -> int:
        """How many consecutive repair attempts this row is the end of.

        Zero for an attempt branched from the integration head, which is what a
        row written before repair bases existed reads as — correct rather than
        merely convenient, since under that code no attempt ever repaired
        anything.
        """
        marker = (self.extra or {}).get(REPAIR_KEY)
        if isinstance(marker, dict):
            length = marker.get("chain_length")
            if isinstance(length, int) and length > 0:
                return length
        return 0

    @property
    def repair_of_attempt(self) -> Optional[int]:
        """The attempt number whose rejected diff this attempt is repairing."""
        marker = (self.extra or {}).get(REPAIR_KEY)
        if isinstance(marker, dict):
            prior = marker.get("attempt_no")
            if isinstance(prior, int):
                return prior
        return None

    @property
    def guidance_key(self) -> Tuple[str, str]:
        """The scope retry guidance is valid within: `(node_id, integration_head)`.

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

        **The integration head and not `base_sha`, and the distinction only
        appeared when repair bases did.** The paragraph above says "when an
        upstream merge advances the integration head" — the head was always the
        fact the expiry was about, and `base_sha` was its proxy only for as
        long as the two were the same string. A repair attempt bases on the
        rejected attempt's output commit, so keying on `base_sha` would mint a
        fresh, empty ledger for the very attempt whose whole purpose is to act
        on the findings in it, and the repair prompt would carry none of them.
        Keyed on the head, a repair chain accumulates guidance across its
        attempts and still expires the instant a sibling merges — which is also
        the instant the repair basis itself is refused, so the two rules cannot
        come apart.
        """
        return (self.node_id, self.integration_head)
