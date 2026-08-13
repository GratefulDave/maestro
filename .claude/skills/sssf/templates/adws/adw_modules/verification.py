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


def adjudicate_gate(result: "wt.GateResult", min_cases: int) -> GateVerdict:
    """Adjudicate a gate execution from the merge protocol.

    The exit code is deliberately not consulted. It is the thing this rule
    replaces: a suite that collected nothing, or skipped everything, exits
    zero and has asserted nothing about the work.
    """
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


# ── §10.1 no guard reads free text ──────────────────────────────────────────

class _FreeTextVisitor(ast.NodeVisitor):

    def __init__(self) -> None:
        self.found: List[str] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FREE_TEXT_FIELDS:
            self.found.append(node.attr)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        index = node.slice
        # Python 3.9 still wraps a constant subscript in ast.Index on some
        # parse paths, so unwrap before testing rather than assuming 3.10.
        if isinstance(index, ast.Index):  # pragma: no cover - version dependent
            index = index.value  # type: ignore[attr-defined]
        if isinstance(index, ast.Constant) and index.value in FREE_TEXT_FIELDS:
            self.found.append(index.value)
        self.generic_visit(node)


def free_text_reads(source: str) -> Tuple[str, ...]:
    """Every read of a §10.1 free-text field in `source`, by AST.

    Parsed rather than grepped, and the distinction is the same one §7.5
    makes about stderr: a field name inside a docstring or a string constant
    is not a read, and a detector that convicted one would be the lexical
    matching this design forbids. Both access shapes are covered, because
    catching only the attribute form would leave the mapping form silently
    permitted.
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
