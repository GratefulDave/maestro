"""§7.5 retry classification — pure policy, no store, no process, no git.

`classify` takes a structural description of one failed attempt — an
exception's type, an exit code, whether a report parsed and what it said, a
computed gate/permission/diff outcome — and returns exactly one of a
`RetryClass` or one of the three block reasons that fit no retry class at all
(§7.3). It never runs anything and never reads a history; anything it needs
about prior attempts is passed in as rows the caller already has, or as a
callable the caller injects. That is what makes this module testable without
a database, and it is why `scheduler_types.py` — the shared vocabulary this
module classifies *into* — is read-only here (per its own docstring, the
classifier is one of the three pieces that must agree on those names without
owning a private copy of any of them).

**Classification is structural, never lexical.** `classify` may read an
exception's type, an exit code, whether a binary resolved, whether the
process started, and whether a report exists and parses. It may not read
stderr or stdout content — no field on `FailureSignal` carries that text, so
the prohibition holds by construction, and the two AST detectors at the
bottom of this file are the executed proof that this module's own code never
routes around that boundary by comparing against process output or by
concluding a git object is absent from anything but the one documented exit
code. Both detectors run against this file's own source as a test, and both
carry a planted-violation fixture proving they go red on a real violation —
a detector never shown to catch anything is not a detector.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from .scheduler_types import (
    AttemptRecord,
    BlockReason,
    DEFAULT_RETRY_CLASS,
    NodeKind,
    REPAIR_KEY,
    RetryClass,
    SchedulerConfig,
)
from .worktree import PermissionVerdict


# ── §7.5 structural inputs — every field a fact, never process output text ──

@dataclass(frozen=True)
class ReportOutcome:
    """Whether a report exists and parses, and what it says — never its text.

    `parsed=False` covers both "no report" and "a report that failed to
    parse": §7.5 draws no distinction between them, because both leave
    `classify` with nothing structural to call semantic.
    """

    parsed: bool
    failed: bool = False


@dataclass(frozen=True)
class GateOutcome:
    """§7.4's two gate runs, agent nodes only — booleans, never gate text."""

    pre_gate_failed: bool
    post_gate_passed: bool


@dataclass(frozen=True)
class CodeEffect:
    """§7.3's code-node clause: exit code and whether the diff is empty."""

    exit_zero: bool
    diff_empty: bool
    expects_changes: bool


class LauncherFailure(str, Enum):
    """§7.5's LAUNCHER_TRANSIENT triggers, partitioned by budget below.

    Members, not classes. §7.5 closes the retry classes at three and makes the
    closure load-bearing, and it also says what varies inside a class: "the
    budget is a property of the *member*, not of the class". CREDENTIAL has
    always been the proof of that — zero retries inside a class named for
    faults a second attempt might survive.

    `DETERMINISTIC_REFUSAL` is the second member of that same shape (§16.3
    item 46). LAUNCHER_TRANSIENT is named for an assumption — that another
    attempt might survive what this one did not — and a refusal that is
    deterministic *by construction* satisfies every structural test for the
    class while violating that assumption: a call site that omits an
    environment omits it identically on every attempt. Without a member for
    it, `ErrorClass.CONFIGURATION` maps to STARTUP, the node makes two more
    launches that cannot succeed, and it blocks LAUNCHER_BUDGET_EXHAUSTED —
    telling an operator a budget ran out when nothing was ever retryable.

    The determinism is stated by the launcher as a typed field on its own
    refusal (`launcher.LaunchRefusal.deterministic`) and travels to the
    classifier as this member. It is never inferred from the refusal's
    message: `LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:...` carries its code in
    prose, and matching that prefix is exactly the lexical shortcut §7.5
    forbids and an AST test convicts.
    """

    PANE_ALLOCATION = "PANE_ALLOCATION"
    STARTUP = "STARTUP"
    TRANSPORT = "TRANSPORT"
    CREDENTIAL = "CREDENTIAL"
    DETERMINISTIC_REFUSAL = "DETERMINISTIC_REFUSAL"


#: The partition §16.3 item 46's discharge requires: the members for which
#: another attempt cannot produce a different answer. `launcher_retry_budget`
#: is its only reader, and `_budget_reason` names the refusal rather than the
#: budget for exactly this set, so a deterministic refusal blocks on its first
#: occurrence saying what was refused.
DETERMINISTIC_LAUNCHER_FAILURES: Tuple[LauncherFailure, ...] = (
    LauncherFailure.CREDENTIAL,
    LauncherFailure.DETERMINISTIC_REFUSAL,
)


@dataclass(frozen=True)
class FailureSignal:
    """Everything `classify` may read about one failed attempt (§7.5).

    `exception_type` is carried for callers that want it in a log line; the
    classifier's own branching never inspects it, because every failure shape
    it names (OS errors, timeouts, git failures, sqlite busy) already falls
    through to the ENVIRONMENTAL default without needing a special case, and
    adding one would be exactly the lexical shortcut §7.5 forbids.
    """

    node_kind: NodeKind
    exception_type: Optional[str] = None
    exit_code: Optional[int] = None
    binary_resolved: bool = True
    process_started: bool = True
    report: Optional[ReportOutcome] = None
    gate: Optional[GateOutcome] = None
    permission: Optional[PermissionVerdict] = None
    code_effect: Optional[CodeEffect] = None
    launcher_failure: Optional[LauncherFailure] = None


@dataclass(frozen=True)
class Classification:
    """Exactly one of the two is set — the same pairing `NodeLifecycle`
    enforces between `block_reason` and the BLOCKED state.

    `reason` is the classifier's own account of the failure, carried into the
    durable failure detail and the retry guidance. It existed as a call-site
    keyword (the reviewer-stall arm) before it existed as a field, which made
    that arm a TypeError waiting for its first stall; the field closes that
    and never participates in the exactly-one-of pairing.

    `launcher_failure` is the typed LAUNCHER_TRANSIENT trigger, carried
    forward from the signal because the budget is not a property of the class
    alone: §7.5 gives CREDENTIAL zero retries and every other launcher failure
    one or two. `launcher_retry_budget` below needs the *member*, not the
    class, and until this field existed it had no production caller at all —
    the zero-retry rule was expressible and unreachable. Like `reason`, it
    takes no part in the exactly-one-of pairing.
    """

    retry_class: Optional[RetryClass] = None
    block_reason: Optional[BlockReason] = None
    reason: Optional[str] = None
    launcher_failure: Optional[LauncherFailure] = None

    def __post_init__(self) -> None:
        if (self.retry_class is None) == (self.block_reason is None):
            raise ValueError(
                "a classification is exactly one of retry_class or block_reason, "
                "never both and never neither")


# ── §7.5 / §7.3 the classifier ───────────────────────────────────────────────

def classify(signal: FailureSignal) -> Classification:
    """Structural, never lexical (§7.5). Evaluated in the order §7.3 and §7.4
    define the underlying predicates: the gate's own falsifiability first
    (§7.4), then the permission check that is measured before anything else
    is even committed (§8.3), then the code node's declared expectation
    (§7.3), then content-level failure (SEMANTIC), then launch failure
    (LAUNCHER_TRANSIENT), and only once nothing structural matched, the
    fail-closed ENVIRONMENTAL default.
    """
    # ── the three failures that fit no retry class at all (§7.3, §7.5) ──────
    if (signal.node_kind is NodeKind.AGENT and signal.gate is not None
            and not signal.gate.pre_gate_failed):
        return Classification(block_reason=BlockReason.GATE_NOT_FALSIFIABLE)

    if signal.permission is not None and not signal.permission.passes:
        if signal.node_kind is NodeKind.CODE:
            return Classification(block_reason=BlockReason.PERMISSION_SCOPE_VIOLATION)
        # An agent node's clause-4 failure is deliberately not in this
        # family: an agent is not deterministic, and a retry prompt naming
        # the offending paths is genuinely new instructions (§7.5).
        return Classification(retry_class=RetryClass.SEMANTIC)

    if signal.node_kind is NodeKind.CODE and signal.code_effect is not None:
        ce = signal.code_effect
        if ce.exit_zero and ce.expects_changes and ce.diff_empty:
            return Classification(block_reason=BlockReason.CODE_NODE_NO_EFFECT)

    # ── SEMANTIC: a parseable failing report, or a failed post-node gate ────
    if signal.report is not None and signal.report.parsed and signal.report.failed:
        return Classification(retry_class=RetryClass.SEMANTIC)

    if (signal.gate is not None and signal.gate.pre_gate_failed
            and not signal.gate.post_gate_passed):
        return Classification(retry_class=RetryClass.SEMANTIC)

    # ── LAUNCHER_TRANSIENT: pane allocation, startup, transport ──────────────
    # The member travels with the class. Returning only the class discards the
    # one fact that distinguishes CREDENTIAL's zero budget from every other
    # launcher failure's one or two, and a budget rule whose input is thrown
    # away here cannot fire anywhere downstream (§7.5).
    if signal.launcher_failure is not None:
        return Classification(retry_class=RetryClass.LAUNCHER_TRANSIENT,
                              launcher_failure=signal.launcher_failure)

    if not signal.binary_resolved or not signal.process_started:
        return Classification(retry_class=RetryClass.LAUNCHER_TRANSIENT)

    # ── the fail-closed default (§7.5 containment) ───────────────────────────
    # No report is never SEMANTIC: a report that exists but did not parse, or
    # no report at all, both fell through every SEMANTIC check above and land
    # here — OS errors, timeouts, git failures, and sqlite busy all belong to
    # this shape, and none of them needed a special case to reach it.
    return Classification(retry_class=DEFAULT_RETRY_CLASS)


def classify_with_containment(build_signal: Callable[[], FailureSignal]) -> Classification:
    """§7.5 containment — the worker body's top-level handler.

    `build_signal` is whatever the caller does to turn a raw failure into a
    `FailureSignal`. If that construction itself raises — an engine bug, not
    a fact about the code under test — the failure defaults to ENVIRONMENTAL,
    fail-closed, never SEMANTIC, and is swallowed rather than propagated: a
    worker failure writes only its own node's state, and a `ThreadPoolExecutor`
    future nobody looks at must never carry the run down with it.
    """
    try:
        return classify(build_signal())
    except Exception:
        return Classification(retry_class=DEFAULT_RETRY_CLASS)


# ── §7.5 the semantic budget, both halves ────────────────────────────────────

def semantic_attempts_at_base(attempts: Iterable[AttemptRecord], node_id: str,
                              base_sha: str) -> int:
    """The `(node_id, base_sha)` prompt-mutation scope (§7.5).

    A new base is genuinely new evidence, so this re-arms with no counter to
    clear and no reset event to fire — it is a `COUNT(*)` over the attempt
    rows that already exist, derived from a stored fact rather than a flag.
    """
    return sum(1 for a in attempts
              if a.node_id == node_id and a.base_sha == base_sha
              and a.retry_class is RetryClass.SEMANTIC
              and not (a.extra or {}).get(REVIEW_REJECTED_KEY))


def semantic_attempts_total(attempts: Iterable[AttemptRecord], node_id: str) -> int:
    """The `(run_id, node_id)` cumulative count, across every base.

    No infra fault ever contributes here: only rows already classified
    SEMANTIC are counted, so an ENVIRONMENTAL or LAUNCHER_TRANSIENT failure
    can never produce a budget decrement (§7.5). Callers pass rows already
    scoped to one run.

    **Review rejections are excluded**, and the exclusion is what keeps the two
    ceilings genuinely separate. A rejected diff is written `SEMANTIC` because
    that is what it is by §7.5's rule — it earns a prompt-mutating retry, and
    the other two classes would be lies about a content verdict. But it is
    counted under `review_ceiling`, so leaving it in this sum would spend both
    budgets for one failure: a node rejected twice by review would arrive at
    its third gate failure already out of semantic attempts, which is the
    merged counter the two ceilings exist to avoid. The row says what the
    attempt *was*; the marker says which budget it *spent*.
    """
    return sum(1 for a in attempts
              if a.node_id == node_id and a.retry_class is RetryClass.SEMANTIC
              and not (a.extra or {}).get(REVIEW_REJECTED_KEY))


# ── the review budget, counted the same way and kept separate ───────────────

#: The marker `fail_attempt`/`mark_blocked` write into `attempts.extra_json`
#: when an attempt was recycled because a reviewer rejected its diff. A key in
#: a row the count already ranges over, rather than a fourth `RetryClass`,
#: because §7.5 states three mutually exclusive classes and every guard in the
#: system is written against exactly those three.
REVIEW_REJECTED_KEY = "review_rejected"

#: Findings-per-attempt, stored on the same review-rejected extra row the
#: budget already counts. A rejected diff is only half the operator's
#: question — the other half is whether the reviewer is finding *less* each
#: time, which is what says another attempt would land and a flat series says
#: it would not. The count lives on the durable row rather than in process
#: memory so a resumed run reports the same series the original would have.
REVIEW_FINDINGS_COUNT_KEY = "review_findings_count"


def review_attempts_total(attempts: Iterable[AttemptRecord], node_id: str) -> int:
    """`COUNT(*)` over this node's review-rejected attempt rows.

    Scoped to `(run_id, node_id)` across every base, exactly as
    `semantic_attempts_total` is, and for the same reason: without the
    cumulative scope every unrelated merge mints a new base and re-arms a
    per-base counter, so total spend would scale with the number of merges
    rather than with the node.

    Deliberately disjoint from the semantic count. A red gate and a rejected
    diff are different failures — one says the stated behaviour is absent, the
    other says the code that produces it should not merge — and a shared budget
    would let a node that burned two attempts on gate failures merge unreviewed
    on its third because the reviewer had no attempts left to spend.
    """
    return sum(1 for a in attempts
               if a.node_id == node_id
               and bool((a.extra or {}).get(REVIEW_REJECTED_KEY)))


def review_attempts_across_runs(
        attempts_by_run: Mapping[str, Iterable[AttemptRecord]],
        node_id: str) -> Tuple[int, Tuple[str, ...]]:
    """This node's review-rejected attempts summed over many runs, and the
    runs that hold them.

    The cumulative scope `review_attempts_total` establishes stops at the run
    boundary, and a node can be re-attempted past its ceiling simply by
    starting the plan again: each fresh `run_id` mints a `node_lifecycle` row
    with an empty history, so the reviewer's verdict on the same node against
    the same plan bytes is spent over and over with nothing counting it. That
    is the debt #92 names — spend no amount of success pays off — accruing one
    run at a time.

    It counts by calling `review_attempts_total` per run rather than by
    restating its predicate. RC1 is the recorded cost of the alternative: a
    second copy of the review budget rule lived in
    `retry_policy.review_budget_exhausted`, had no production caller, and
    disagreed with the enforced one by exactly one attempt. One predicate,
    counted in more places.

    The run ids come back beside the total because the refusal that reads this
    has to name them — an operator told a node is out of attempts and not told
    where they went has to go and find the runs themselves — and deriving them
    from a second pass would be the same duplication one level up. Ordering
    follows the caller's mapping, which is the reader's newest-first run order,
    and only runs that actually hold a rejection appear.
    """
    total = 0
    holders: List[str] = []
    for run_id, attempts in attempts_by_run.items():
        counted = review_attempts_total(attempts, node_id)
        if counted:
            total += counted
            holders.append(run_id)
    return total, tuple(holders)


def review_convergence_from_attempts(
        attempts: Iterable[AttemptRecord]) -> Dict[str, List[int]]:
    """Rebuild findings-per-attempt per node from durable review-rejected rows.

    Ordered by `attempt_no` so a resumed process reports the same series the
    original process appended in memory. A row written before this key
    existed carries no count and is skipped rather than counted as zero — a
    zero would read as "the reviewer found nothing", which is the one thing a
    review-rejected row cannot mean.
    """
    by_node: Dict[str, List[Tuple[int, int]]] = {}
    for attempt in attempts:
        extra = attempt.extra or {}
        if not extra.get(REVIEW_REJECTED_KEY):
            continue
        count = extra.get(REVIEW_FINDINGS_COUNT_KEY)
        if count is None:
            continue
        by_node.setdefault(attempt.node_id, []).append(
            (attempt.attempt_no, int(count)))
    return {
        node_id: [count for _no, count in sorted(items)]
        for node_id, items in by_node.items()
    }


# ── the repair basis: a rejected diff is repaired, not re-implemented ────────
#
# A review rejection classifies SEMANTIC and recycles the attempt, and until
# this section existed the recycled attempt branched from the integration head
# like every other attempt — which discards the rejected diff entirely and asks
# the builder to implement the node again from an empty tree. Consecutive
# attempts were therefore not iterations of one artifact but independent
# implementations, each judged fresh, and findings could not descend because
# nothing accumulated: one production node produced 2, 2, 1, 3 findings over
# four rejections, all four attempts based on the same commit, rewriting 573
# lines each time and deleting the 1-finding version rather than repairing it.
# `review_ceiling` was six chances to guess right in one shot, never six rounds
# of refinement.
#
# This is issue #90's shape applied to the other actor. There, a *reviewer*
# that failed was re-dispatched against the attempt's surviving output commit
# rather than the builder being discarded; here, a builder whose diff was
# rejected is sent back to that same surviving commit rather than to an empty
# tree. Both decisions read the same three kinds of fact — a git object name, a
# count of durable rows, and a member of a closed vocabulary — and neither
# reads a word of the reviewer's prose (§1.2). The findings travel in the
# prompt, where `render_guidance` already puts them and where nothing
# transitions on them.

#: The git object name of the commit a rejected attempt actually produced,
#: stored on the rejected attempt's own row beside the marker that says it was
#: rejected. `node_lifecycle.output_sha` cannot serve: it is written by
#: `mark_verified`, which a rejected attempt never reaches, so on this path the
#: lifecycle row holds nothing at all. The attempt's durable ref does hold the
#: commit, but a ref read alone proves only what the ref points at *now* — it
#: cannot say whether that is still what the harness committed. Two facts,
#: compared, are what make the subject of a repair provable, exactly as
#: `ReviewStallFacts` keeps `output_sha` and `surviving_sha` as two fields.
REVIEW_OUTPUT_SHA_KEY = "review_output_sha"

#: At most this many *consecutive* repair attempts before the chain breaks and
#: the node is re-derived from the integration head.
#:
#: In code and not in `maestro.config.yaml`: the config is deployment-owned and
#: deliberately not mirrored between the template and its deployments, so a key
#: added there would exist in one copy of the runtime and not the others.
#:
#: Three, and the number matters less than the fact that it is a constant
#: compared against a count of durable rows. Each admitted repair strictly
#: increases `chain_length`, so no chain can exceed it; a broken chain restarts
#: from the integration head at `chain_length` zero, and the *number* of chains
#: is bounded by `review_ceiling + granted`, which this section does not touch.
#: The loop therefore adds no attempts at all — it only changes what an attempt
#: the review budget had already paid for starts from.
REPAIR_CHAIN_LIMIT = 3

#: Why a rejected diff was not repaired. Module constants, never strings
#: composed at the call site, for the reason `classify_review_stall`'s reasons
#: are: the value is written into a durable row and an operator who greps for
#: it needs a closed set to grep.
REPAIR_NO_PRIOR_REJECTION = "the previous attempt was not rejected by review"
REPAIR_OUTPUT_UNPROVEN = (
    "the rejected attempt has no provable output commit to repair")
REPAIR_HEAD_MOVED = (
    "the integration head moved while this node was being reviewed, so the "
    "rejected diff and the findings about it are both stale")
REPAIR_CHAIN_EXHAUSTED = (
    "the repair chain reached its limit without the diff being accepted")
REPAIR_FINDINGS_ROSE = (
    "the last repair raised more findings than the rejection it repaired")
REPAIR_ADMITTED = "repairing the rejected diff"


@dataclass(frozen=True)
class RepairFacts:
    """The typed facts one repair decision reads (§1.2).

    Every field is a git object name, an integer, a boolean derived from git's
    own object database, or a member of `RetryClass`. Nothing here is pane
    text, prompt text, a free-text envelope field, or an agent's claim about
    its own work — the reviewer's prose reaches the *prompt* and is read
    nowhere else, and the reviewer's verdict reaches this decision only as the
    typed marker on the attempt row and as an integer count of findings.

    `prior` is the node's highest-numbered attempt row, which is the attempt
    the one being opened directly succeeds.

    `rejected_ref_sha` is what that attempt's own durable ref holds *now*, read
    back from git rather than remembered, and `output_proven` is
    `worktree.is_attempt_output_commit` over it: the shape test, the object
    test, the descent-from-its-own-base test, and the exact-ref test, all four.
    A repair that branched from anything else would be repairing a tree the
    evidence chain does not name.

    `repaired_findings` is the findings count of the rejection that `prior`
    itself was repairing, or `None` when `prior` repaired nothing or when the
    count was never stored. `None` is "unknown" and never zero, for the reason
    `review_convergence_from_attempts` skips it: zero would read as "the
    reviewer found nothing", which is the one thing a rejection cannot mean.
    """

    integration_head: str
    prior: Optional[AttemptRecord]
    rejected_ref_sha: Optional[str] = None
    output_proven: bool = False
    repaired_findings: Optional[int] = None


@dataclass(frozen=True)
class RepairBasis:
    """What an admitted repair changes about the attempt being opened."""

    #: The commit the attempt's worktree branches from — the rejected diff.
    base_sha: str
    #: The integration head it is nevertheless derived from (§8.1). Equal to
    #: the head this decision was taken against, and recorded because
    #: `base_sha` can no longer carry it.
    integration_head: str
    #: The attempt whose rejected diff is being repaired.
    repair_of_attempt: int
    #: Including this one. Bounded by `REPAIR_CHAIN_LIMIT`.
    chain_length: int


@dataclass(frozen=True)
class RepairDecision:
    """Exactly one basis or none, with the reason the ledger records for it."""

    basis: Optional[RepairBasis]
    reason: str


def review_output_extra(output_sha: str) -> Dict[str, Any]:
    """The rejected attempt's output commit, as its own row stores it."""
    return {REVIEW_OUTPUT_SHA_KEY: output_sha}


def repair_extra(basis: RepairBasis) -> Dict[str, Any]:
    """The repair marker, as the *new* attempt's row stores it.

    `base_sha` is deliberately absent: the attempt row already has a `base_sha`
    column holding exactly that value, and a second copy would be the two
    representations §4 convicts. Every key written here has a reader (§3.6
    B15) — `AttemptRecord.integration_head` reads `integration_head`,
    `AttemptRecord.repair_chain_length` reads `chain_length`, and
    `AttemptRecord.repair_of_attempt` reads `attempt_no`, which
    `repaired_findings_count` follows to find the rejection this chain's
    progress is measured against.
    """
    return {REPAIR_KEY: {
        "attempt_no": basis.repair_of_attempt,
        "integration_head": basis.integration_head,
        "chain_length": basis.chain_length,
    }}


def repaired_findings_count(
        attempts: Iterable[AttemptRecord],
        prior: Optional[AttemptRecord]) -> Optional[int]:
    """The findings count of the rejection `prior` was itself repairing.

    `None` when `prior` repaired nothing, when the attempt it names is not
    among the rows given, or when that attempt's row predates
    `REVIEW_FINDINGS_COUNT_KEY`. Unknown, never zero.
    """
    if prior is None:
        return None
    repaired_no = prior.repair_of_attempt
    if repaired_no is None:
        return None
    for attempt in attempts:
        if attempt.node_id != prior.node_id or attempt.attempt_no != repaired_no:
            continue
        count = (attempt.extra or {}).get(REVIEW_FINDINGS_COUNT_KEY)
        return int(count) if isinstance(count, int) else None
    return None


def decide_repair(facts: RepairFacts) -> RepairDecision:
    """Whether the attempt being opened repairs the rejected diff, or restarts.

    Five refusals, then the admission. Each refusal is structural, and the
    order is the order in which a fact becomes knowable rather than a
    preference:

    1. **The previous attempt was not rejected by review.** Both halves are
       read: the row's `retry_class` must be SEMANTIC *and* it must carry
       `REVIEW_REJECTED_KEY`. Either alone is the wrong predicate — a red gate
       is SEMANTIC without being a rejection, and the marker without the class
       would admit a row `mark_blocked` wrote. A gate failure leaves nothing to
       repair: the tree it produced never passed its own gate, so the next
       attempt restarts from the integration head exactly as it always has. An
       ENVIRONMENTAL or LAUNCHER_TRANSIENT failure fails the same test and is
       untouched by any of this.
    2. **The rejected diff is not provable.** No stored output commit, a ref
       that no longer holds it, or a commit that fails
       `is_attempt_output_commit` means the subject of the repair cannot be
       named, and branching from a tree the evidence chain does not name is
       worse than starting over.
    3. **The integration head moved.** §8.1 puts attempt worktrees on the
       integration head, and a repair honours that by branching from a commit
       that *descends* from the head the rejection was measured against — which
       is only the current head while no sibling has merged. When one has, the
       rejected commit descends from a head that is no longer integration's,
       and branching from it would silently hand the builder a tree missing the
       sibling's work. The same equality also expires the findings, and it is
       the expiry `AttemptRecord.guidance_key` already performs, so the two
       cannot disagree: a base the guidance is invalid at is a base the repair
       is refused at.
    4. **The chain reached `REPAIR_CHAIN_LIMIT`.** A rejected diff that is
       fundamentally wrong must not be repaired forever; after three
       consecutive repairs the node is re-derived from the integration head
       with the findings still in the prompt.
    5. **The last repair made it worse.** Findings rising across a repair — the
       repaired rejection raised fewer than the repair did — is the ledger
       saying the diff in hand is not the thing to keep. Both counts are
       integers stored on rows the review budget already counts; an unknown
       count never breaks the chain, because "unknown" is not "rose".

    Nothing here can block a node. Every refusal falls back to the behaviour
    that existed before this function did, which is the fresh base the
    scheduler would have used anyway.
    """
    prior = facts.prior
    if (prior is None
            or prior.retry_class is not RetryClass.SEMANTIC
            or not (prior.extra or {}).get(REVIEW_REJECTED_KEY)):
        return RepairDecision(None, REPAIR_NO_PRIOR_REJECTION)
    rejected_sha = (prior.extra or {}).get(REVIEW_OUTPUT_SHA_KEY)
    if (not isinstance(rejected_sha, str) or not rejected_sha
            or not facts.output_proven
            or facts.rejected_ref_sha != rejected_sha):
        return RepairDecision(None, REPAIR_OUTPUT_UNPROVEN)
    if prior.integration_head != facts.integration_head:
        return RepairDecision(None, REPAIR_HEAD_MOVED)
    chain_length = prior.repair_chain_length + 1
    if chain_length > REPAIR_CHAIN_LIMIT:
        return RepairDecision(None, REPAIR_CHAIN_EXHAUSTED)
    prior_findings = (prior.extra or {}).get(REVIEW_FINDINGS_COUNT_KEY)
    if (isinstance(prior_findings, int)
            and facts.repaired_findings is not None
            and prior_findings > facts.repaired_findings):
        return RepairDecision(None, REPAIR_FINDINGS_ROSE)
    return RepairDecision(
        RepairBasis(base_sha=rejected_sha,
                    integration_head=facts.integration_head,
                    repair_of_attempt=prior.attempt_no,
                    chain_length=chain_length),
        REPAIR_ADMITTED)


# ── §7.5 the reviewer-side failure: retry the reviewer, not the builder ──────

#: Every reviewer dispatch made against one attempt's committed output, stored
#: on that attempt's own row. A list rather than a counter, because the count
#: is not the only thing the ledger owes an operator: which route and model
#: stalled, on which signal, and after how long is the difference between "the
#: reviewer is wedged" and "this commit is too large for that context window",
#: and neither is recoverable from a number.
#:
#: It lives on the attempt row rather than in process memory for the reason
#: `GUIDANCE_KEY` does: the budget below is a `COUNT(*)` over durable rows, and
#: a resumed scheduler that counted an in-memory list would re-arm the budget
#: at every process boundary — which is the refund loop §7.5 convicts, arrived
#: at through a counter that looks local and is not.
REVIEW_DISPATCH_KEY = "review_dispatches"


class ReviewDispatchOutcome(str, Enum):
    """What a reviewer-side failure earns.

    `REDISPATCH` sends the *reviewer* at the same commit again. `SETTLE` hands
    the failure back to the ENVIRONMENTAL default, which is what the
    reviewer-stall arm did unconditionally before this existed — and doing it
    unconditionally is issue #90: the builder's committed, gate-passing tree
    was discarded and the node re-derived from base, four times over, for a
    failure that was never the builder's.
    """

    REDISPATCH = "REDISPATCH"
    SETTLE = "SETTLE"


#: Why a reviewer-side failure was not re-dispatched. Module constants, never
#: strings composed at the call site: `_settle_failure` writes the value into
#: the durable failure detail, and a reason an operator greps for has to be
#: one of a closed set to be greppable at all. The decision below returns one
#: of these *beside* its outcome; nothing ever branches on the text.
#:
#: `REVIEW_REDISPATCH_EXHAUSTED` is deliberately the wording the stall arm has
#: always written. A run that exhausts its re-dispatches ends exactly where it
#: ended before, and an operator's existing query for that reason keeps
#: finding it.
REVIEW_OUTPUT_UNPROVEN = "the attempt has no provable output commit to re-review"
REVIEW_REDISPATCH_EXHAUSTED = "the code reviewer stalled without reporting"
REVIEW_REDISPATCH_NO_HEADROOM = (
    "the code reviewer stalled and the attempt window cannot hold another "
    "review of the same length")


@dataclass(frozen=True)
class ReviewDispatchDecision:
    """Exactly one outcome, with the reason the ledger records for it."""

    outcome: ReviewDispatchOutcome
    reason: str


@dataclass(frozen=True)
class ReviewStallFacts:
    """The typed facts one reviewer-side failure carries (§1.2).

    Every field is either a git object name, a number, or a member of a closed
    vocabulary the reviewer's own machinery defines. Nothing here is pane text,
    prompt text, or a free-text envelope field, and nothing here is an agent's
    claim about its own work — a stalled reviewer produced no claim, which is
    precisely what makes this a fact about the machine.

    `surviving_sha` is what the attempt's own git ref holds *now*, read back
    after the stall rather than remembered from before it. `output_sha` is what
    the harness committed. They are two fields and not one on purpose: equal
    means the builder's work is still exactly where the evidence chain says it
    is, and anything else — a moved ref, a missing ref — means the subject of
    the re-review cannot be proven and the re-dispatch is refused.
    """

    output_sha: Optional[str]
    surviving_sha: Optional[str]
    dispatches_spent: int
    budget: int
    #: `finalization_window.FinalizationSignal`'s value — the reviewer window's
    #: own typed account of how it ended. Carried for the ledger; the decision
    #: below never branches on it.
    signal: Optional[str] = None
    route: Optional[str] = None
    model: Optional[str] = None
    session_id: Optional[str] = None
    elapsed_s: Optional[float] = None
    #: Seconds left in this attempt's §7.6 window when the stall was observed.
    #: `None` means the caller could not measure it, which is not the same as
    #: zero and is treated as "unmeasured" rather than as "none left".
    window_headroom_s: Optional[float] = None


def review_dispatches_spent(attempt: Optional[AttemptRecord]) -> int:
    """`COUNT(*)` over the typed dispatch rows this attempt already stored.

    Scoped to the attempt rather than to the node, because that is the scope
    the fact is true within: a dispatch spent re-reviewing attempt a4's commit
    says nothing about a5's, and counting across attempts would let one
    attempt's wedged reviewer spend the next attempt's allowance.

    A row written before this key existed carries no list and counts zero,
    which is correct rather than merely convenient: under that code no
    dispatch beyond the first was ever made.
    """
    if attempt is None:
        return 0
    stored = (attempt.extra or {}).get(REVIEW_DISPATCH_KEY)
    if not isinstance(stored, list):
        return 0
    return len(stored)


def review_dispatch_row(facts: "ReviewStallFacts") -> Dict[str, Any]:
    """One dispatch record, as the attempt row stores it.

    `dispatch_no` is derived from the count rather than passed in, so the
    stored sequence cannot disagree with the budget that counts it.
    """
    return {
        "output_sha": facts.output_sha,
        "surviving_sha": facts.surviving_sha,
        "dispatch_no": facts.dispatches_spent + 1,
        "signal": facts.signal,
        "route": facts.route,
        "model": facts.model,
        "session_id": facts.session_id,
        "elapsed_s": facts.elapsed_s,
    }


def classify_review_stall(facts: "ReviewStallFacts") -> ReviewDispatchDecision:
    """§7.5 applied to the actor that actually failed.

    A reviewer that never reported says nothing about the code, which is why
    the stall arm has always classified ENVIRONMENTAL. What it also says
    nothing about is the *builder*, and that half was missing: the whole
    attempt was failed, so a committed tree that had passed its post-node gate
    and its permission check was discarded and the node re-derived from the
    integration head. Issue #90 measured the cost — four attempts, four
    accepted envelopes, four passing gates, zero verdicts, and a node blocked
    `ENVIRONMENTAL_BUDGET_EXHAUSTED` having never once been reviewed — and the
    growth it causes: each restart replays the retry guidance ledger, so every
    cycle produced a larger implementation of the same requirement.

    The retry that matches the failure is another *reviewer* against the same
    commit. Three facts decide it, all structural:

    1. There is an output commit, and the attempt's own ref still holds it.
       Without that the subject of the re-review is unproven, and re-reviewing
       something other than what the evidence chain names is worse than not
       re-reviewing at all.
    2. The re-dispatch budget is not spent. It is the ENVIRONMENTAL budget —
       the class this failure already belonged to — spent on re-dispatching
       the reviewer rather than on discarding the builder.
    3. The attempt's §7.6 window can still hold a review of the length the one
       that just stalled took. Without this the re-dispatch converts a
       reviewer stall into a `NODE_TIMEOUT` and discards the same commit one
       horizon later, which is the failure this function exists to stop.

    None of the three reads text. `surviving_sha` is `git rev-parse` over the
    attempt's own ref, `dispatches_spent` is a count of durable rows, and the
    headroom is arithmetic over two numbers the attempt row and the config
    already carry.
    """
    if not facts.output_sha or facts.surviving_sha != facts.output_sha:
        return ReviewDispatchDecision(
            ReviewDispatchOutcome.SETTLE, REVIEW_OUTPUT_UNPROVEN)
    if facts.dispatches_spent >= facts.budget:
        return ReviewDispatchDecision(
            ReviewDispatchOutcome.SETTLE, REVIEW_REDISPATCH_EXHAUSTED)
    if (facts.window_headroom_s is not None and facts.elapsed_s is not None
            and facts.window_headroom_s < facts.elapsed_s):
        return ReviewDispatchDecision(
            ReviewDispatchOutcome.SETTLE, REVIEW_REDISPATCH_NO_HEADROOM)
    return ReviewDispatchDecision(
        ReviewDispatchOutcome.REDISPATCH, REVIEW_REDISPATCH_EXHAUSTED)


# ── guidance, made durable ───────────────────────────────────────

#: The extra key each entry is stored under on the attempt that produced it.
#: The ledger was process-local, so every standing constraint died with the
#: scheduler process and a resumed run told its builder nothing about why the
#: previous attempts were rejected. Writing the entry onto the row that
#: already records the failure keeps one representation of it, not two.
GUIDANCE_KEY = "guidance"


def guidance_extra_verification(detail: Optional[dict]) -> Dict[str, Any]:
    """The typed VERIFICATION entry, as the attempt row stores it."""
    guidance = verification_guidance(detail)
    return {GUIDANCE_KEY: {
        "surface": "verification",
        "reason": guidance.reason,
        "offending_paths": list(guidance.offending_paths),
        "failed_clause": guidance.failed_clause,
        "unreferenced_symbols": list(guidance.unreferenced_symbols),
    }}


def guidance_extra_review(review: object) -> Dict[str, Any]:
    """The typed REVIEW entry, as the attempt row stores it."""
    guidance = review_guidance(review)
    return {GUIDANCE_KEY: {
        "surface": "review",
        "subject_digest": guidance.subject_digest,
        "findings": [
            {"check_id": finding.check_id, "object_id": finding.object_id,
             "message": finding.message, "blocking": finding.blocking}
            for finding in guidance.findings
        ],
    }}


def guidance_from_attempts(
        attempts: Iterable[AttemptRecord]
        ) -> Dict[Tuple[str, str], "GuidanceLedger"]:
    """Rebuild every node's ledger from the durable attempt extras.

    Keyed by `AttemptRecord.guidance_key` — `(node_id, base_sha)` — because
    that is the scope the guidance itself is valid within: a finding about a
    diff taken against one base says nothing about a diff taken against
    another. Replayed in attempt order so the reconstructed history reads the
    same way the in-process one accumulated, and empty ledgers are dropped so
    a resumed scheduler's map contains exactly the nodes that have standing
    constraints.
    """
    ledgers: Dict[Tuple[str, str], GuidanceLedger] = {}
    for attempt in sorted(attempts,
                          key=lambda item: (item.node_id, item.attempt_no)):
        payload = (attempt.extra or {}).get(GUIDANCE_KEY)
        if not isinstance(payload, dict):
            continue
        key = attempt.guidance_key
        ledger = ledgers.get(key, GuidanceLedger())
        surface = payload.get("surface")
        if surface == "verification":
            ledger = ledger.with_verification(VerificationGuidance(
                reason=str(payload.get("reason") or ""),
                offending_paths=tuple(payload.get("offending_paths") or ()),
                failed_clause=payload.get("failed_clause"),
                unreferenced_symbols=tuple(
                    payload.get("unreferenced_symbols") or ())))
        elif surface == "review":
            findings = tuple(
                ReviewFinding(
                    check_id=str(item.get("check_id") or ""),
                    object_id=str(item.get("object_id") or ""),
                    message=str(item.get("message") or ""),
                    blocking=bool(item.get("blocking")),
                )
                for item in (payload.get("findings") or [])
                if isinstance(item, dict)
            )
            ledger = ledger.with_review(ReviewGuidance(
                subject_digest=str(payload.get("subject_digest") or ""),
                findings=findings))
        else:
            continue
        ledgers[key] = ledger
    return {key: ledger for key, ledger in ledgers.items() if not ledger.empty}


# ── §7.5 launcher budgets — credential is the zero-retry exception ──────────

def launcher_retry_budget(cfg: SchedulerConfig, failure: Optional[LauncherFailure]) -> int:
    """LAUNCHER_TRANSIENT's budget, sized from the member (§7.5).

    Two members spend nothing. CREDENTIAL's zero is §7.5's own entry, the one
    row in the retry table whose whole purpose is to *not* retry.
    DETERMINISTIC_REFUSAL's zero is the same rule applied to the same class:
    the refusal is a property of the configuration or the plan rather than of
    the moment, so an identical relaunch produces an identical refusal, and
    the two doomed attempts buy only a misleading block reason.

    CREDENTIAL keeps reading its configured value rather than being folded
    into the literal below, because it is an operator-settable number that
    happens to default to zero. A deterministic refusal is not a budget at
    all — there is nothing for an operator to raise — so it is a constant.
    """
    if failure is LauncherFailure.CREDENTIAL:
        return cfg.credential_retries
    if failure in DETERMINISTIC_LAUNCHER_FAILURES:
        return 0
    return cfg.launcher_retries


# ── retry guidance ledger — one slot per acceptance surface ─────────────────
#
# A node is judged by more than one independent acceptance surface: the §8.3
# verification predicate (clause 4's permission check runs before the commit
# even exists) and the cross-vendor reviewer's located findings. A retry
# prompt that carries only the most recent failure lets the surfaces take
# turns rejecting the node: run-32b19abadf4a4d6b801ae0f4456976c7 oscillated
# for five attempts because each attempt fixed exactly what the last prompt
# named and silently regressed the constraint the prompt no longer mentioned.
# Each individual fix was correct; the two constraints were never held
# simultaneously.
#
# The ledger holds the latest typed evidence from *each* surface and renders
# all of it into every retry prompt. Three properties are deliberate:
#
# * **Typed, not concatenated prose.** The state is dataclasses keyed by
#   surface, never an accumulated string. §10.1/§1.2 still hold by
#   construction — nothing transitions on this state; it is rendered into the
#   prompt and read nowhere else. The prompt is the one place free text is
#   allowed to travel, because a prompt is not a guard: §10.1 forbids a
#   *lifecycle transition* caused by free text, and every transition here was
#   caused by the derived verdict or signed receipt before any of this
#   rendering existed.
# * **Latest-per-surface replacement is the retirement rule.** A finding
#   retires when its own surface re-evaluates a newer attempt and no longer
#   reports it — the new entry replaces the old, and a fixed defect simply
#   does not reappear in the new evidence. A constraint never retires because
#   the *other* surface failed, and never because one attempt satisfied it:
#   the run above BLOCKED precisely because a satisfied constraint vanished
#   from the prompt and the next attempt regressed it. Standing constraints
#   are cheap to restate; a dropped one costs an attempt.
# * **Bounded (A9/B13).** At most one entry per surface, each replaced rather
#   than appended, so ledger size is bounded by the newest verdict plus the
#   newest review, and rendering truncates deterministically to a character
#   budget without ever dropping a surface. Attempt counts stay bounded by the
#   untouched semantic and review ceilings; the ledger adds no loop.
#
# A new acceptance surface later means a new enum member and a new slot on the
# ledger — the same scoping rule §1.1 item 4 applies to evidence chains, so a
# new surface extends the structure rather than borrowing another's slot.

class AcceptanceSurface(str, Enum):
    """The independent predicates that can reject an attempt and recycle it.

    One member per surface that mutates the retry prompt. `VERIFICATION` is
    the §8.3 predicate (clauses 1–4, including the pre-commit permission
    check); `REVIEW` is the cross-vendor reviewer. Launcher and environmental
    failures never mutate the prompt (§7.5), so they have no member here.
    """

    VERIFICATION = "verification"
    REVIEW = "review"


@dataclass(frozen=True)
class VerificationGuidance:
    """The latest verification failure, in the fields §8.3 already produces."""

    reason: str = ""
    offending_paths: Tuple[str, ...] = ()
    failed_clause: Optional[int] = None
    #: Located `path:line:name` records for symbols the attempt defined that
    #: nothing references (#118). Carried separately from `offending_paths`
    #: because the two are different facts with different repairs — one names
    #: where the attempt wrote, the other names what it must delete — and a
    #: prompt that conflated them would ask for the wrong edit.
    unreferenced_symbols: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewFinding:
    """One located finding, copied from the derived verdict's graded cell."""

    check_id: str
    object_id: str
    message: str
    blocking: bool


@dataclass(frozen=True)
class ReviewGuidance:
    """The latest review rejection: which cells failed, where, and why."""

    subject_digest: str
    findings: Tuple[ReviewFinding, ...] = ()


@dataclass(frozen=True)
class GuidanceLedger:
    """Every typed entry per acceptance surface, for one node, in order.

    Immutable; `with_*` returns a new ledger that *appends* to that surface
    and carries every other surface forward. Carrying the other surface
    forward is what stopped a review rejection from erasing what verification
    said. Appending within a surface is the rest of the same fix: the slot
    held one entry, so a second finding from the same surface overwrote the
    first, and a builder that had failed twice was told about one of the two
    failures. "Newer evidence supersedes older" was the stated justification
    and it does not hold — the surfaces re-evaluate a *different diff* each
    attempt, so a constraint absent from the latest evaluation may be absent
    because the attempt stopped writing the file it was about, not because it
    was satisfied. History is kept and `render_guidance` bounds it; the
    truncation there is deterministic and visible, which an overwrite was
    not.
    """

    verification: Tuple[VerificationGuidance, ...] = ()
    review: Tuple[ReviewGuidance, ...] = ()

    def with_verification(self, guidance: VerificationGuidance) -> "GuidanceLedger":
        return GuidanceLedger(
            verification=self.verification + (guidance,),
            review=self.review)

    def with_review(self, guidance: ReviewGuidance) -> "GuidanceLedger":
        return GuidanceLedger(
            verification=self.verification,
            review=self.review + (guidance,))

    @property
    def empty(self) -> bool:
        return not self.verification and not self.review


def verification_guidance(detail: Optional[dict]) -> VerificationGuidance:
    """Build the VERIFICATION entry from the scheduler's typed failure detail.

    `detail` is the `_failure_detail` record the scheduler already computes
    once per failed attempt and writes to every row that records the failure
    — `reason` (the classifier's), `clause`, `verdict` (the predicate's own
    reason), `offending_paths` — with empty keys omitted. Consuming that one
    record here, rather than re-deriving the same facts from the verdict,
    keeps a single representation of the failure instead of two that can
    drift (§4's conviction, applied to this module's own inputs).
    """
    detail = detail or {}
    reason = detail.get("verdict") or detail.get("reason") or ""
    return VerificationGuidance(
        reason=str(reason),
        offending_paths=tuple(detail.get("offending_paths") or ()),
        failed_clause=detail.get("clause"),
        unreferenced_symbols=tuple(detail.get("unreferenced_symbols") or ()))


def review_guidance(review: object) -> ReviewGuidance:
    """Copy the located findings out of a review verdict's graded cells.

    Blocking findings first, advisories after, each carrying the same three
    facts `findings_text` renders: which cell, on which object, and why. The
    prose message rides along as data — it is rendered into the prompt and
    read nowhere else.
    """
    findings: List[ReviewFinding] = []
    for cell in getattr(review, "findings", ()) or ():
        findings.append(ReviewFinding(
            check_id=cell.check_id, object_id=cell.object_id,
            message=cell.message.strip(), blocking=True))
    for cell in getattr(review, "advisories", ()) or ():
        findings.append(ReviewFinding(
            check_id=cell.check_id, object_id=cell.object_id,
            message=cell.message.strip(), blocking=False))
    return ReviewGuidance(
        subject_digest=getattr(review, "subject_digest", ""),
        findings=tuple(findings))


#: Rendering budget for the whole guidance block, in characters. B13's lesson
#: applies to the builder as much as the reviewer: a prompt that outgrows the
#: agent's context produces an attempt about a different task. The budget is
#: enforced by deterministic truncation that keeps every surface's header and
#: check ids and elides message prose first, with an explicit marker — a
#: silently dropped surface is the oscillation bug again.
GUIDANCE_CHAR_BUDGET = 12_000

_TRUNCATION_MARKER = (
    "  [guidance truncated to fit the prompt budget; every constraint named "
    "above still binds in full]")

#: Written in place of the entries an accumulating ledger could not afford.
#: A surface's history is dropped oldest-first, so what this marker replaces
#: is always older than what survives it.
_HISTORY_MARKER = (
    "  [earlier findings from this surface dropped to fit the prompt budget; "
    "the entries below are the most recent]")


#: How many offending paths the retry prompt names individually.
#:
#: The whole list used to be joined into one line, which is unbounded in the
#: only place a bound matters: a permission failure whose delta was measured
#: over a whole dependency tree rendered 16090 paths as a single 1.1MB line.
#: `_fit` could then do nothing with it but drop it entirely — the surface
#: header survived, the paths did not, and the prompt told the agent that it
#: had failed clause 4 without naming one path it wrote. Unbounded and useless
#: at the same time.
#:
#: Twenty, because a breach is shaped like a directory rather than a scatter:
#: twenty entries show the prefix, the depth, and whether more than one root is
#: involved, which is what an agent can actually act on, and they cost ~2KB
#: against a 12,000-character budget the review surface also draws from. One
#: path per line so that `_fit` can trim the sample from the end and still
#: leave the count and the first entries standing.
OFFENDING_PATH_SAMPLE = 20


def _offending_path_lines(paths: Sequence[str]) -> List[str]:
    """The offending paths, bounded, with the total preserved as a count.

    The count is a structural fact about the measured delta and survives
    whatever the sample elides — an elision that silently changed "16090" into
    "20" would be the prompt lying about the size of the breach.
    """
    total = len(paths)
    shown = list(paths[:OFFENDING_PATH_SAMPLE])
    lines = ["Paths written outside this node's declared outputs "
             f"({total} in total):"]
    lines.extend("  " + path for path in shown)
    if total > len(shown):
        lines.append(f"  ... and {total - len(shown)} more, elided here; the "
                     "full list is recorded in this attempt's failure detail")
    return lines


def _unreferenced_symbol_lines(symbols: Sequence[str]) -> List[str]:
    """Every unreachable symbol, in full, as `path:line:name`.

    Not sampled, unlike the offending paths above. That elision is safe because
    a permission breach is repaired by writing elsewhere and the count is the
    fact that matters; this list is the work itself, and an agent handed twenty
    of thirty deletes twenty, ships again, and spends another attempt on the
    ten the prompt elided.
    """
    lines = [f"Symbols this node defined that nothing references "
             f"({len(symbols)} in total). Each must be removed, or given a "
             f"caller that a test exercises:"]
    lines.extend("  " + symbol for symbol in symbols)
    return lines


def _verification_lines(node: object, g: VerificationGuidance) -> List[str]:
    lines = ["Verification (§8.3):",
             "A prior attempt for this node did not verify."
             if g.failed_clause is None else
             "A prior attempt for this node did not verify "
             f"(clause {g.failed_clause})."]
    if g.reason:
        lines.append(g.reason)
    if g.offending_paths:
        lines.extend(_offending_path_lines(g.offending_paths))
    if g.unreferenced_symbols:
        lines.extend(_unreferenced_symbol_lines(g.unreferenced_symbols))
    lines.append("Declared outputs: "
                 + (", ".join(getattr(node, "outputs", ()) or ()) or "(none)"))
    return lines


def _review_lines(g: ReviewGuidance) -> List[str]:
    lines = ["Code review:",
             "A prior attempt for this node passed its gate but was rejected "
             "by code review. The gate going green is not the bar; the "
             "findings below must be resolved and the gate kept green."]
    blocking = [f for f in g.findings if f.blocking]
    advisory = [f for f in g.findings if not f.blocking]
    if blocking:
        lines.append("Blocking findings — each must be resolved:")
        for f in blocking:
            lines.append(f"  [{f.object_id}] {f.check_id}")
            lines.append(f"    {f.message}")
    if advisory:
        lines.append("Advisory findings — address if you agree:")
        for f in advisory:
            lines.append(f"  [{f.object_id}] {f.check_id}")
            lines.append(f"    {f.message}")
    return lines


def _fit(lines: List[str], share: int) -> List[str]:
    """Deterministically truncate one surface's section to its share.

    Drops lines from the end until the section plus the marker fits; the
    first line (the surface header) always survives, so a surface is never
    silently absent from the prompt.
    """
    def size(ls: List[str]) -> int:
        return sum(len(l) + 1 for l in ls)

    if size(lines) <= share:
        return lines
    kept = list(lines)
    while len(kept) > 1 and size(kept) + len(_TRUNCATION_MARKER) + 1 > share:
        kept.pop()
    return kept + [_TRUNCATION_MARKER]


def _join(blocks: List[List[str]]) -> List[str]:
    """One surface's per-attempt blocks, blank-line separated, oldest first."""
    lines: List[str] = []
    for block in blocks:
        if lines:
            lines.append("")
        lines.extend(block)
    return lines


def _fit_surface(blocks: List[List[str]], share: int) -> List[str]:
    """Fit one surface's accumulated history into that surface's share.

    `_fit` alone is the wrong tool once a surface carries more than one
    entry: it drops trailing lines, and with history rendered oldest-first
    the trailing lines are the *newest* finding — the one the next attempt
    has to fix. So whole entries are dropped from the front first, newest
    always last to go, and the drop is announced rather than silent. Only
    when a single entry still overflows does `_fit` truncate within it,
    which is the pre-history behaviour and keeps the surface's header.
    """
    def size(lines: List[str]) -> int:
        return sum(len(l) + 1 for l in lines)

    kept = list(blocks)
    dropped = 0
    reserve = len(_HISTORY_MARKER) + 1
    while len(kept) > 1 and size(_join(kept)) + reserve > share:
        kept.pop(0)
        dropped += 1
    lines = _fit(_join(kept), share - (reserve if dropped else 0))
    return ([_HISTORY_MARKER, ""] + lines) if dropped else lines


#: The preamble that turns an implement prompt into a repair prompt.
#:
#: A repair prompt is not the implement prompt with findings appended, and the
#: difference is not tone. The implement prompt's first line is the node's
#: `instruction`, and an agent handed that plus a list of findings reads a
#: request to build the node — which is exactly what it did before, and exactly
#: what produced a fresh 573-line implementation each attempt. The instruction
#: still bounds the work and is still the first thing in the prompt; what these
#: lines add is the one fact the prompt could not otherwise carry: the work
#: already exists in the tree the agent is looking at, and the task is to
#: change it rather than to write it.
#:
#: The commit is named because an agent that wants to see what it is repairing
#: can read the diff, and because a named commit is checkable — a prompt that
#: said "your previous work is here" without saying which commit would be a
#: claim the agent could not verify. Nothing transitions on any of this
#: (§10.1/§1.2): every lifecycle decision about the repair was already taken by
#: `decide_repair` over typed rows before this text was rendered.
def _repair_lines(basis: "RepairBasis") -> List[str]:
    return [
        "REPAIR, NOT REIMPLEMENTATION.",
        "This attempt's working tree is not empty and does not start from the "
        "integration head. It starts from commit {0} — the diff a previous "
        "attempt of this node produced, which passed its gate and was then "
        "rejected by code review.".format(basis.base_sha),
        "The work is already there. Do not write this node again from "
        "scratch: read what is in the tree, and change it so that the findings "
        "below no longer hold. A rewrite discards the parts the reviewer did "
        "not object to and is judged as a new diff, which is what previous "
        "attempts did and why the same objections kept recurring.",
        "The instruction at the top of this prompt still bounds the work — it "
        "says what this node must do, and the findings below say what is wrong "
        "with the code in hand. Both bind. Your diff is still judged in full "
        "against the integration head {0}, not against commit {1}, so every "
        "term the instruction states must still hold when you are "
        "done.".format(basis.integration_head, basis.base_sha),
    ]


def render_guidance(node: object, ledger: Optional[GuidanceLedger],
                    char_budget: int = GUIDANCE_CHAR_BUDGET,
                    repair: Optional["RepairBasis"] = None) -> Optional[str]:
    """Every surface's standing constraints, rendered for the retry prompt.

    This string is the one place the ledger is ever read. It mutates the
    prompt — which is what makes a SEMANTIC retry genuinely new instructions
    (§7.5) — and nothing transitions on it (§10.1/§1.2).

    Same-surface history renders oldest-first inside that surface's section,
    so a later attempt still reads every prior finding from it. The per-
    section budget is unchanged, which is what keeps an accumulating ledger
    from growing the prompt without bound: `_fit` truncates within a section
    and marks that it did, so history is what gets dropped under pressure and
    a whole surface never silently disappears (B13).

    `repair` is the typed basis `decide_repair` admitted, or `None` for the
    ordinary fresh-base retry. It changes the preamble and nothing else: the
    findings below are the same findings either way, and the repair preamble
    is what tells the agent they are about code it is looking at rather than
    code it is about to write. It renders **before** the budget is divided
    among the surfaces and is never truncated, because a repair prompt whose
    repair instruction was elided is an implement prompt.
    """
    if ledger is None or ledger.empty:
        return None
    sections: List[List[List[str]]] = []
    if ledger.verification:
        sections.append([_verification_lines(node, item)
                         for item in ledger.verification])
    if ledger.review:
        sections.append([_review_lines(item) for item in ledger.review])
    preamble = [
        "Every constraint below is binding at the same time. Earlier attempts "
        "were rejected for fixing one surface while regressing another; a "
        "constraint a prior attempt already satisfied still binds."]
    if repair is not None:
        preamble = _repair_lines(repair) + [""] + preamble
    share = max(0, (char_budget - sum(len(l) + 1 for l in preamble))
                ) // len(sections)
    rendered: List[str] = list(preamble)
    for section in sections:
        rendered.append("")
        rendered.extend(_fit_surface(section, share))
    return "\n".join(rendered)


# ── §7.5 git results: only the documented not-found exit code is a fact ─────

class GitResult(str, Enum):
    """"No report can ever be semantic" applied to git rather than to agents:
    only git's own documented not-found exit code means the object is absent.
    Every other nonzero exit is ENVIRONMENTAL, never a fact about the
    repository."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    ENVIRONMENTAL_FAILURE = "ENVIRONMENTAL_FAILURE"


def classify_git_exit(exit_code: int, not_found_exit_code: int) -> GitResult:
    """§7.5 — the one-line fix: absence is an equality check against the
    documented not-found code, and nothing else is ever read as absence. A
    transient git failure must never be recorded as a missing object, because
    every eligibility obligation that reads git objects would then silently
    return a wrong answer instead of failing.
    """
    if exit_code == 0:
        return GitResult.PRESENT
    if exit_code == not_found_exit_code:
        return GitResult.ABSENT
    return GitResult.ENVIRONMENTAL_FAILURE


# ── §7.5 AST detector #1: no comparison against process output text ─────────

#: Identifiers that denote a process's own output bytes, matched **exactly**.
#: `text` was a member and is deliberately not: it is the most generic string
#: name in Python and denoted process output only by local convention, so it
#: convicted newline normalisation (`text.endswith("\n")`) in three modules
#: and a markdown-fence check in a fourth, while the four names below are
#: unambiguous. The cost of dropping it is stated rather than hidden — see
#: `find_output_content_comparisons`.
_STDIO_NAME_MARKERS = ("stderr", "stdout", "output", "tail")
_STDIO_COMPARISON_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
_STDIO_COMPARISON_METHODS = ("startswith", "endswith", "find", "lower", "upper")


def _looks_like_output(node: ast.AST) -> bool:
    """Whether this operand denotes a process's own output bytes.

    **Exact identifier match, never a substring.** The substring form this
    replaces convicted `node.outputs` — a plan node's *declared output paths*,
    which are harness-computed state and exactly what §7.5 permits a
    classifier to read — because "outputs" contains "output". Nine such hits
    across the tree, every one a false positive on an ordinary membership test
    like `selector in node.outputs`, and they are why this rule could only
    ever be pointed at its own module.

    Narrowing the input rather than exempting the callers is the whole point:
    a rule that needs an allowlist to widen has stopped describing what it is
    about. `outputs` is not `output`; the plural is a different concept, not a
    special case of the singular.
    """
    if isinstance(node, ast.Name):
        return node.id.lower() in _STDIO_NAME_MARKERS
    if isinstance(node, ast.Attribute):
        return node.attr.lower() in _STDIO_NAME_MARKERS
    return False


class _OutputComparisonVisitor(ast.NodeVisitor):
    """Walks a module looking for string comparison, `in`, or
    `.startswith`/`.find`/`.lower()`-style calls against a value that looks
    derived from process output. This is the executed proof behind "an AST
    test forbids string comparison against process output" (§7.5)."""

    def __init__(self) -> None:
        self.violations: List[Tuple[int, str]] = []

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        if any(_looks_like_output(operand) for operand in operands):
            for op in node.ops:
                if isinstance(op, _STDIO_COMPARISON_OPS):
                    self.violations.append(
                        (node.lineno, "string/in comparison against process output"))
                    break
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr in _STDIO_COMPARISON_METHODS
                and _looks_like_output(node.func.value)):
            self.violations.append(
                (node.lineno, f".{node.func.attr}() against process output"))
        self.generic_visit(node)


def find_output_content_comparisons(source: str) -> List[Tuple[int, str]]:
    """Every place `source` compares against or pattern-matches process output.

    §7.5: `_classify` "may **not** read stderr or stdout content; an AST test
    forbids string comparison against process output, because a regex over
    stderr is how ecosystem specifics leak into a general engine."

    **Runs over the whole tree, not one module.** It was scoped to
    `retry_policy.py` alone, which is the narrowest possible reading of a rule
    about the engine's behaviour, and the same single-module shape that let a
    live violation sit unseen in `plan_validate.py` under a detector written
    for exactly that bug. Widening needed no allowlist — only an input
    narrowed to what the rule is actually about (`_looks_like_output`).

    **A stated limit, because the widening bought it at a price.** The rule
    matches operands *named* like process output. Output bound to a generic
    local first — `text = str(error).lower()`, then `"locked" not in text` —
    is invisible to it, and that construct exists at
    `coordinator_store.py:288`, where a sqlite failure is classified by
    matching its message rather than its error code. That is an instance of
    this rule's own shape which this rule does not catch; it is reported
    rather than papered over with a marker so generic it convicted three
    modules' newline handling. Closing it needs dataflow, not another name.
    """
    tree = ast.parse(source)
    visitor = _OutputComparisonVisitor()
    visitor.visit(tree)
    return visitor.violations


#: A small, exact violation of the rule above: a classifier reading stderr
#: text. The detector must be proven to catch this, or it proves nothing.
PLANTED_OUTPUT_COMPARISON_FIXTURE = '''
def classify(exc, stderr):
    if "timeout" in stderr:
        return "ENVIRONMENTAL"
    if stderr.startswith("fatal:"):
        return "ENVIRONMENTAL"
    return "SEMANTIC"
'''


# ── §7.5 AST detector #2: no unclassified git failure is a repository fact ──

_ABSENCE_MARKERS = ("absent", "not_found", "notfound", "missing")


def _mentions_absence(body: List[ast.stmt]) -> bool:
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name) and any(m in sub.id.lower() for m in _ABSENCE_MARKERS):
                return True
            if isinstance(sub, ast.Attribute) and any(
                    m in sub.attr.lower() for m in _ABSENCE_MARKERS):
                return True
    return False


def _gated_by_equality(test: ast.expr) -> bool:
    """Whether an `if` test is a single `==` comparison — the only shape
    that can legitimately gate a conclusion of absence (§7.5)."""
    return (isinstance(test, ast.Compare) and len(test.ops) == 1
           and isinstance(test.ops[0], ast.Eq))


class _GitAbsenceVisitor(ast.NodeVisitor):
    """Walks a module for an `if` branch that concludes "absent" without an
    equality-gated not-found check — a `!= 0` or bare truthy exit-code test
    treats every nonzero git exit as a repository fact, exactly the
    misclassification "no report can ever be semantic" forbids when applied
    to git (§7.5)."""

    def __init__(self) -> None:
        self.violations: List[Tuple[int, str]] = []

    def visit_If(self, node: ast.If) -> None:
        if _mentions_absence(node.body) and not _gated_by_equality(node.test):
            self.violations.append(
                (node.lineno,
                 "absence concluded without an equality-gated not-found check"))
        self.generic_visit(node)


def find_ungated_git_absence(source: str) -> List[Tuple[int, str]]:
    """Parse `source` and return every branch that concludes git-object
    absence without gating on equality to a documented not-found code."""
    tree = ast.parse(source)
    visitor = _GitAbsenceVisitor()
    visitor.visit(tree)
    return visitor.violations


#: A small, exact violation: treating any nonzero exit as absence.
PLANTED_GIT_ABSENCE_FIXTURE = '''
def check(exit_code):
    if exit_code != 0:
        return GitResult.ABSENT
    return GitResult.PRESENT
'''
