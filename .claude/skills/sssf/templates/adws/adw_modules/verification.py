"""The VERIFIED predicate, the one counting rule, and result adjudication.

Three things live here because all three are the same mistake if they live
apart: each is a place where "it looked fine" was allowed to stand in for
"it was checked."

* **§10.2's counting rule.** The base's test gate was a pure exit-code check
  and could not detect a suite that ran zero tests, and the quality layer
  beneath it returned an echo command that always exits zero. One shared
  parser produces `(collected, passed, failed, skipped, errored)` and one
  rule adjudicates it: `passed >= min_cases >= 1 AND skipped < collected AND
  errored == 0`. No lexical blocklist is needed and none is permitted — an
  echo collects zero cases and is caught structurally.

* **§7.3's VERIFIED predicate**, which is defined *per node kind*. The four
  agent clauses came from §7.4, §8.3, and §10.2, where each carried an
  agent-only scope that naming them into one predicate silently dropped.
  Unscoped, clause 2 and clause 3 are unsatisfiable for a code node — it has
  no gate and no `min_cases` — so a code node could never verify, never
  merge, and would wedge its subtree, breaking §6.7's own recommended
  composition. Both predicates are therefore written out separately.

* **§7.7's adjudication**, which binds a result to the attempt row it names
  and retains the payload in all four outcomes. An adjudication recorded
  without its payload is how a correct FAIL carrying two real findings
  disappeared behind a byte-identical journal.

What this module does not do: it never runs a gate, never touches git, and
never classifies an exception. It is a pure adjudicator over evidence the
caller measured, which is what makes every clause testable without a
subprocess.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Tuple

from . import scheduler_types as st
from . import worktree as wt

#: §10.1 — guards may read `status`, `artifacts`, and `changed_files`. Prose is
#: permitted as input to work, never as a guarantee about work.
FREE_TEXT_FIELDS: Tuple[str, ...] = ("notes_for_next_agent", "summary")

#: How a runner's summary names each outcome. `error`/`errors` are folded by
#: the merge protocol's parser already; both spellings are accepted here so
#: this module never depends on which one produced the mapping.
_ERROR_KEYS = ("errored", "error", "errors")


# ── §10.2 one shared parser, one shared rule ────────────────────────────────

@dataclass(frozen=True)
class GateCounts:
    """`(collected, passed, failed, skipped, errored)` — the only shape the
    rule consumes, whether the gate was a node gate or integration
    acceptance."""

    collected: int
    passed: int
    failed: int
    skipped: int
    errored: int

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Optional["GateCounts"]:
        """Read a runner's counts, or `None` when nothing parsed.

        `None` is not "zero tests" — it is "no report", and §10.2 sends the
        two to different places: no report is fail-closed and environmental,
        while zero passing cases is a red gate about the work. Collapsing
        them would let a runner that failed to start be recorded as a verdict.
        """
        if not raw:
            return None
        found = False
        values = {}
        for name, keys in (("passed", ("passed",)), ("failed", ("failed",)),
                           ("skipped", ("skipped",)), ("errored", _ERROR_KEYS)):
            value = 0
            for key in keys:
                if key in raw:
                    value = int(raw[key])
                    found = True
                    break
            values[name] = value
        if not found:
            return None
        # An explicit total wins. Deriving it from the four outcome counts is
        # correct because a summary line partitions the collected set, and
        # refusing to derive would fail every runner that names outcomes
        # without naming the total — but a runner that collected ten and
        # reported three outcomes lost seven, and the rule must see the loss
        # rather than smooth it into a smaller denominator.
        collected = int(raw["collected"]) if "collected" in raw else sum(values.values())
        return cls(collected=collected, **values)


@dataclass(frozen=True)
class GateVerdict:
    """One gate, adjudicated. `unparseable` is the distinction §10.2 draws
    between a failing suite and a suite that never reported."""

    green: bool
    unparseable: bool
    counts: Optional[GateCounts]
    reason: str = ""
    #: Set only when the failure is infrastructural. A parsed red report is
    #: content, so it carries no retry class here — §7.5's classifier owns
    #: that decision and must not find it pre-made.
    retry_class: Optional[st.RetryClass] = None


def adjudicate_counts(raw: Mapping[str, Any], min_cases: int) -> GateVerdict:
    """§10.2's rule: `passed >= min_cases >= 1 AND skipped < collected AND
    errored == 0`.

    `min_cases >= 1` is enforced here rather than trusted, because a gate
    demanding zero cases is a gate that cannot fail, which is precisely the
    vacuity this rule exists to catch.
    """
    if min_cases < 1:
        raise ValueError(
            "min_cases >= 1 (§10.2): a gate that demands zero passing cases "
            "cannot fail, which is the vacuity the counting rule exists to catch")

    counts = GateCounts.parse(raw)
    if counts is None:
        return GateVerdict(
            green=False, unparseable=True, counts=None,
            reason="no parseable report — fail-closed and environmental (§10.2)",
            retry_class=st.RetryClass.ENVIRONMENTAL)

    if counts.errored:
        reason = f"{counts.errored} errored"
    elif counts.failed:
        # DIVERGENCE FROM §10.2 AS WRITTEN, and it is deliberate. The section
        # states the rule as `passed >= min_cases >= 1 AND skipped < collected
        # AND errored == 0` — `failed` appears in the parser's five-tuple and
        # in no clause of the conjunction. Taken literally, a node whose own
        # scoped gate reports one pass and one failure satisfies the rule at
        # `min_cases = 1` and reaches VERIFIED with its declared behaviour
        # demonstrably absent, which contradicts §7.3 clause 3's purpose and
        # §7.4's whole falsifiability argument: the post-gate is supposed to
        # witness that this node's behaviour is present, and a failing case in
        # the node's own scope is exactly the witness that it is not.
        # Implemented as the conjunction plus `failed == 0`; §10.2 needs the
        # clause added and the defect registered in §16.3.
        reason = f"{counts.failed} failed"
    elif counts.passed < min_cases:
        reason = f"{counts.passed} passed, min_cases is {min_cases}"
    elif counts.skipped >= counts.collected:
        reason = f"{counts.skipped} skipped of {counts.collected} collected"
    else:
        return GateVerdict(green=True, unparseable=False, counts=counts)
    return GateVerdict(green=False, unparseable=False, counts=counts, reason=reason)


def adjudicate_pre_gate(result: "wt.GateResult", min_cases: int,
                        selector_unbuilt: bool = False) -> GateVerdict:
    """Adjudicate the pre-node gate, where an absent selector is the red.

    A node that creates its own test file has no test file at its base, so its
    pre-gate selector names a path that does not exist and the runner refuses
    to collect -- pytest exits 4, emitting no counts at all. Read through the
    ordinary rule that is an unparseable gate, hence ENVIRONMENTAL, and the
    attempt is retried until the budget is gone; every retry starts from the
    same base and fails identically.

    An absent selector is not a broken environment. It is the strongest form
    of the red that clause 2 requires: the behaviour cannot be present,
    because the file asserting it has not been written yet. `selector_unbuilt`
    is passed by the caller, which is the only layer that knows the selector
    names a path this node is declared to produce.
    """
    if selector_unbuilt and result.exit_code != 0:
        return GateVerdict(
            green=False, unparseable=False, counts=GateCounts.parse(result.counts),
            reason=("gate exited {} with its selector absent at base, which is "
                    "this node's declared output".format(result.exit_code)))
    return adjudicate_gate(result, min_cases)


def adjudicate_gate(result: "wt.GateResult", min_cases: int) -> GateVerdict:
    """Adjudicate a gate execution from the merge protocol.

    A nonzero exit is always red. A zero exit is still insufficient: counts
    must satisfy §10.2. Exit code is never a substitute for the counting rule.
    """
    if result.exit_code != 0:
        counts = GateCounts.parse(result.counts)
        return GateVerdict(
            green=False, unparseable=counts is None, counts=counts,
            reason="gate exited {}".format(result.exit_code),
            retry_class=st.RetryClass.ENVIRONMENTAL if counts is None else None)
    return adjudicate_counts(result.counts, min_cases)


# ── §7.3 the VERIFIED predicate, per node kind ──────────────────────────────

@dataclass(frozen=True)
class VerificationVerdict:
    """Why a node is or is not VERIFIED, in terms the scheduler can act on.

    `block_reason` is set only for the failures §7.5 says no retry class
    fits. `retry_class` is set only where this predicate can settle the class
    structurally — an agent's clause-4 failure is SEMANTIC by §7.5's own
    rule, and an unparseable gate is ENVIRONMENTAL by §10.2's. Everything
    else is left `None` for the classifier, so this module never pre-empts it.
    """

    verified: bool
    failed_clause: Optional[int] = None
    reason: str = ""
    block_reason: Optional[st.BlockReason] = None
    retry_class: Optional[st.RetryClass] = None
    offending_paths: Tuple[str, ...] = ()

    @property
    def asserts_repository_wide(self) -> bool:
        """Always false, and named so the bound cannot be assumed away.

        VERIFIED asserts that this node's declared behaviour is present at
        this base and that its writes were authorized. Reading it as the
        wider claim is the conflation §7.3 exists to prevent, one level up.
        """
        return False


def _permission_paths(permission: "wt.PermissionVerdict") -> Tuple[str, ...]:
    return tuple(permission.conjunct1_violations) + tuple(permission.conjunct2_violations)


def verify_agent_node(envelope_parsed: bool,
                      pre_gate: GateVerdict,
                      post_gate: GateVerdict,
                      permission: "wt.PermissionVerdict") -> VerificationVerdict:
    """§7.3's four clauses for an agent node.

    1. the terminal envelope **parses** as a typed envelope (§10.1);
    2. the **pre-node gate FAILED** at this attempt's actual base (§7.4);
    3. the **post-node gate PASSED** under §10.2's counting rule;
    4. the worktree delta passes §8.3's two-conjunct permission check.

    **The clauses are not evaluated in list order**, and the order below is
    the execution order rather than the numbering: clause 2 runs before the
    agent does, clause 4 is evaluated at measurement with the commit
    following it immediately, and clause 3 is evaluated afterwards against
    the committed tree (§8.4). A node failing both 3 and 4 must report 4,
    because that is the one that stopped the attempt before a commit existed.

    The predicate's reach is bounded. A green result witnesses that this
    node's declared behaviour is present at this base and that its writes
    were authorized. It does not witness that the repository still passes as
    a whole — that claim is made once, by the integration gate at the final
    head (§8.8), and no node's own gate can be asked to make it (§7.4).
    """
    if not envelope_parsed:
        return VerificationVerdict(
            verified=False, failed_clause=1,
            reason="the terminal envelope did not parse as a typed envelope")

    # Clause 2 first: the pre-gate ran before the agent, so a green one stops
    # the attempt before any delta exists to check.
    if pre_gate.unparseable:
        return VerificationVerdict(
            verified=False, failed_clause=2, reason=pre_gate.reason,
            retry_class=st.RetryClass.ENVIRONMENTAL)
    if pre_gate.green:
        return VerificationVerdict(
            verified=False, failed_clause=2,
            reason=("the pre-node gate passed at this attempt's base, so it "
                    "cannot witness this node's behaviour (§7.4)"),
            block_reason=st.BlockReason.GATE_NOT_FALSIFIABLE)

    # Clause 4 next: measured at settle, and the commit follows it at once.
    if not permission.passes:
        paths = _permission_paths(permission)
        return VerificationVerdict(
            verified=False, failed_clause=4,
            reason="the measured delta failed §8.3's permission check",
            # An agent is not deterministic, and a retry prompt naming the
            # offending paths is genuinely new instructions — so unlike the
            # code-node case this is SEMANTIC rather than a block (§7.5).
            retry_class=st.RetryClass.SEMANTIC,
            offending_paths=paths)

    # Clause 3 last: against the committed tree.
    if post_gate.unparseable:
        return VerificationVerdict(
            verified=False, failed_clause=3, reason=post_gate.reason,
            retry_class=st.RetryClass.ENVIRONMENTAL)
    if not post_gate.green:
        return VerificationVerdict(
            verified=False, failed_clause=3, reason=post_gate.reason)

    return VerificationVerdict(verified=True)


def verify_code_node(exit_code: int,
                     permission: "wt.PermissionVerdict",
                     diff_empty: bool,
                     expects_changes: bool = False) -> VerificationVerdict:
    """§7.3's code-node predicate. Clauses 1-3 do not apply.

    A code node's acceptance is its exit code (§6.2), its diff's conformance
    to §8.3's two conjuncts, and its **declared expectation**. The last one
    is authored rather than inferred because diff-emptiness cannot itself
    discriminate a broken codemod from an idempotent one: §6.7 prescribes a
    mechanical node running a formatter for work with no machine-assertable
    post-condition, and on an already-formatted repo that node exits zero
    with an empty diff, byte-for-byte indistinguishable at runtime from the
    misconfigured codemod the check is aimed at.
    """
    if exit_code != 0:
        return VerificationVerdict(
            verified=False, reason=f"the node's command exited {exit_code}")

    if not permission.passes:
        return VerificationVerdict(
            verified=False,
            reason="the measured delta failed §8.3's permission check",
            # A deterministic command that wrote outside its declaration is a
            # plan defect, in the outputs or in the command, and re-running it
            # cannot write different paths — so unlike the agent case this is
            # non-retryable and operator-terminal (§7.5).
            block_reason=st.BlockReason.PERMISSION_SCOPE_VIOLATION,
            offending_paths=_permission_paths(permission))

    if expects_changes and diff_empty:
        return VerificationVerdict(
            verified=False,
            reason=("the node declared expects_changes and produced an empty "
                    "diff; re-running a deterministic command against an "
                    "unchanged base cannot produce a different answer"),
            block_reason=st.BlockReason.CODE_NODE_NO_EFFECT)

    return VerificationVerdict(verified=True)


# ── §7.3's review-node predicate lives in the review path, not here ─────────
#
# `verify_review_node` stood here as a second expression of §7.3's five-clause
# review predicate and was never called by production. Its clauses were already
# enforced, each by the code that owns the observation:
#
#   1. report parsed          `cr.CodeReviewerReport.model_validate` — which
#                             also refuses a finding carrying no grade, no
#                             message, or no reason for its grade (§3.6 A9/B8)
#   2. survived the matrix    `fin.verify_report`
#   3. occupancy measured     `fin.check_occupancy` (NULL convicts, §6.5)
#   4. code derived PASS      `cr.grade_verdict` + `cr.require_located_findings`
#   5. signed receipt         `fin.ReceiptStore` verifies the Ed25519 signature
#                             over the persisted bytes on load
#
# Clauses 1 and 4 name `code_review`'s own report model and derivation rather
# than `finalization`'s. That is the isolation A9's grading is confined by:
# plan finalization keeps `fin.ReviewerReport` and `fin.derive_verdict`, which
# have no grade to read and no threshold to be handed, so grading a node's
# findings cannot change a plan's verdict.
#
# all sequenced by `code_review.review_attempt`, whose `ReviewOutcome.passed` is
# what the scheduler's review branch actually reads. Two expressions of one rule
# with only one of them running is how the two drift, so the unexercised copy
# went rather than the enforced one.
#
# OWED, round 2: the mirror of this comment at the scheduler's review branch,
# naming where each clause is enforced. A deleted predicate with no pointer at
# the call site is how the next person re-derives it from scratch.


# ── §10.1 no guard reads free text ──────────────────────────────────────────

def _free_text_names(node: ast.AST) -> List[str]:
    """Every §10.1 free-text field read anywhere inside `node`.

    Both access shapes, because catching only the attribute form would leave
    the mapping form silently permitted. Parsed rather than grepped: a field
    name inside a docstring or a string constant is not a read, and a detector
    that convicted one would be the lexical matching this design forbids.
    """
    found: List[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in FREE_TEXT_FIELDS:
            found.append(sub.attr)
        elif isinstance(sub, ast.Subscript):
            index = sub.slice
            # Python 3.9 still wraps a constant subscript in ast.Index on some
            # parse paths, so unwrap before testing rather than assuming 3.10.
            if isinstance(index, ast.Index):  # pragma: no cover - version dependent
                index = index.value  # type: ignore[attr-defined]
            if isinstance(index, ast.Constant) and index.value in FREE_TEXT_FIELDS:
                found.append(index.value)
    return found


class _FreeTextVisitor(ast.NodeVisitor):
    """Free-text reads that reach a **decision**, not every read.

    §1.2 forbids a *lifecycle transition* caused by free text. It does not
    forbid reading free text — §7.6's own ledger records it, an error message
    quotes it, and the console prints it, all of which the rule permits and
    all of which the previous form convicted. That is why this guard could
    only ever be pointed at `verification.py`: run tree-wide it reported three
    hits, every one a tracer payload, an f-string in a `raise`, or a console
    line, and the only way to widen a rule that convicts permitted code is an
    allowlist — which is how a guard dies.

    So the rule now asks the question §1.2 actually asks: does this value
    **decide** anything? A free-text read convicts when it sits inside a
    branch test, a comparison, a boolean operator, an assertion, or a
    comprehension filter — the places a value changes what happens next. It
    acquits when the value only flows into a call argument or a formatted
    string, which is recording and display.

    **A stated limit.** This is a syntactic reach, not dataflow: free text
    assigned to a local and branched on later escapes it. Closing that needs
    reaching-definitions analysis, and the honest move is to name the gap
    rather than to widen the shape until it convicts the innocent again.
    """

    def __init__(self) -> None:
        self.found: List[str] = []

    def _decide(self, node: Optional[ast.AST]) -> None:
        if node is not None:
            self.found.extend(_free_text_names(node))

    def visit_If(self, node: ast.If) -> None:
        self._decide(node.test)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._decide(node.test)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._decide(node.test)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._decide(node.test)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for operand in (node.left, *node.comparators):
            self._decide(operand)
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        for value in node.values:
            self._decide(value)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        for condition in node.ifs:
            self._decide(condition)
        self.generic_visit(node)

    def visit_Match(self, node: "ast.Match") -> None:  # pragma: no cover
        self._decide(node.subject)
        self.generic_visit(node)


def free_text_reads(source: str) -> Tuple[str, ...]:
    """Every §10.1 free-text field that **decides** something in `source`.

    §1.2's rule has the widest scope in the specification — a run fails if ANY
    lifecycle transition is caused by pane text, prompt text, a free-text
    envelope field, or an agent's claim — and lifecycle transitions happen in
    `scheduler.py`, not here. Checking the widest rule in the narrowest place
    is what this now stops doing: it runs over the whole tree.

    See `_FreeTextVisitor` for what separates a read from a decision, and for
    the dataflow case this deliberately does not reach.
    """
    visitor = _FreeTextVisitor()
    visitor.visit(ast.parse(source))
    return tuple(visitor.found)


# ── §7.7 results and late arrivals ──────────────────────────────────────────

def adjudicate_result(result: st.ResultRecord,
                      attempts: Iterable[st.AttemptRecord],
                      node_state: Optional[st.NodeState] = None) -> st.ResultRecord:
    """Adjudicate a result **solely against the attempt row it names**.

    Never against the node's current state — `node_state` is accepted so a
    caller cannot be tempted to pre-filter on it, and is deliberately unused
    in the decision. A node already MERGED does not turn a matching live
    attempt's result into a supersession; that conflation is what let a
    correct FAIL be discarded because the node had moved on.

    Four outcomes, and **the payload is retained in all four** — the returned
    row carries both, because they are the same row.
    """
    del node_state  # §7.7 — named to be refused, not read.

    by_number = {a.attempt_no: a for a in attempts}
    named = by_number.get(result.attempt_no)
    if named is None:
        verdict = st.Adjudication.UNKNOWN_ATTEMPT
    elif named.base_sha != result.subject_sha:
        verdict = st.Adjudication.SHA_MISMATCH
    elif named.state is not st.NodeState.RUNNING:
        # The attempt this result names is no longer the live one — a
        # reclaimed attempt's late arrival. Retained, not dropped.
        verdict = st.Adjudication.SUPERSEDED
    else:
        verdict = st.Adjudication.ACCEPTED

    return st.ResultRecord(node_id=result.node_id, attempt_no=result.attempt_no,
                           subject_sha=result.subject_sha, payload=result.payload,
                           adjudication=verdict)
