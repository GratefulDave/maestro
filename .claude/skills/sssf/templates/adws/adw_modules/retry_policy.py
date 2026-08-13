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
from typing import Callable, Iterable, List, Optional, Tuple

from .scheduler_types import (
    AttemptRecord,
    BlockReason,
    DEFAULT_RETRY_CLASS,
    NodeKind,
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
    """§7.5's LAUNCHER_TRANSIENT triggers. CREDENTIAL is distinguished
    because it alone carries a zero retry budget."""

    PANE_ALLOCATION = "PANE_ALLOCATION"
    STARTUP = "STARTUP"
    TRANSPORT = "TRANSPORT"
    CREDENTIAL = "CREDENTIAL"


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
    enforces between `block_reason` and the BLOCKED state."""

    retry_class: Optional[RetryClass] = None
    block_reason: Optional[BlockReason] = None

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
    if signal.launcher_failure is not None:
        return Classification(retry_class=RetryClass.LAUNCHER_TRANSIENT)

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
              and a.retry_class is RetryClass.SEMANTIC)


def semantic_attempts_total(attempts: Iterable[AttemptRecord], node_id: str) -> int:
    """The `(run_id, node_id)` cumulative count, across every base.

    No infra fault ever contributes here: only rows already classified
    SEMANTIC are counted, so an ENVIRONMENTAL or LAUNCHER_TRANSIENT failure
    can never produce a budget decrement (§7.5). Callers pass rows already
    scoped to one run.
    """
    return sum(1 for a in attempts
              if a.node_id == node_id and a.retry_class is RetryClass.SEMANTIC)


def semantic_budget_exhausted(cfg: SchedulerConfig, node_id: str,
                              attempts: Iterable[AttemptRecord],
                              granted_extra_attempts: int = 0) -> bool:
    """§7.5's cumulative ceiling: at most `K + granted` SEMANTIC attempts per
    `(run_id, node_id)` across all bases, closing the refund loop that the
    per-base scope alone leaves unbounded — every unrelated merge mints a new
    base and re-arms the per-base scope, so without this ceiling total spend
    scales with the number of merges rather than with the node.

    `granted_extra_attempts` is read from the node's lifecycle row
    (`NodeLifecycle.granted_extra_attempts`) — the authority tier, never the
    audit tier, per §5.3's runtime-read allowlist — and grants exactly one
    attempt beyond `K` per `retry --force` invocation without raising the cap.
    """
    total = semantic_attempts_total(attempts, node_id)
    allowance = cfg.semantic_ceiling + granted_extra_attempts
    return total >= allowance


# ── §7.5 launcher budgets — credential is the zero-retry exception ──────────

def launcher_retry_budget(cfg: SchedulerConfig, failure: Optional[LauncherFailure]) -> int:
    """LAUNCHER_TRANSIENT's budget, except CREDENTIAL, which is zero (§7.5)."""
    if failure is LauncherFailure.CREDENTIAL:
        return cfg.credential_retries
    return cfg.launcher_retries


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

_STDIO_NAME_MARKERS = ("stderr", "stdout", "output", "text", "tail")
_STDIO_COMPARISON_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)
_STDIO_COMPARISON_METHODS = ("startswith", "endswith", "find", "lower", "upper")


def _looks_like_output(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return any(marker in node.id.lower() for marker in _STDIO_NAME_MARKERS)
    if isinstance(node, ast.Attribute):
        return any(marker in node.attr.lower() for marker in _STDIO_NAME_MARKERS)
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
    """Parse `source` and return every place it compares against, or
    pattern-matches, a value that looks derived from process output. Run
    against this file's own source as a test, and against
    `PLANTED_OUTPUT_COMPARISON_FIXTURE` to prove the detector actually goes
    red on a real violation.
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
