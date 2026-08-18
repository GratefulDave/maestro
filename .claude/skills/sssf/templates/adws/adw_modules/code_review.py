"""Code review as a derived node kind: the diff a build lane produced, read
by a model that did not write it, before anything merges.

Until this module existed a run merged a lane's code on **test results alone**.
Gates prove a stated behaviour is present at a base; nothing read the diff, so
every failure a passing test cannot see — a scope-creeping change, a plausible
implementation of the wrong thing, a hardcoded return that satisfies the one
assertion written for it — merged green. B12 is the recorded shape of that
gap: the integrator/fixer path could declare `verified` on the strength of its
own acceptance re-run, and `quality_gate.review` was presence-checked and never
read by any runner.

**What this module reuses, and why it is not a second implementation.**
`finalization` already owns the whole review protocol: a rubric whose severity
lives in code, a matrix computed in code so two reviewers get identical
coverage, a report schema in which `verdict` and `severity` are
*unrepresentable*, canary cells that convict a reviewer who is not reading,
an occupancy gate that convicts on NULL, verdict derivation in code, and a
create-once Ed25519-signed receipt. Every one of those applies unchanged to a
diff. What is new here is only the *subject*: the objects are a diff and its
changed files instead of a plan and its nodes, and the digest binds a build
attempt instead of plan bytes. Writing a second review pipeline for code would
be the divergence this module exists to avoid.

**Why the receipt keys the transition.** §1.2 fails any run where a lifecycle
transition is caused by pane text, prompt text, a free-text envelope field, or
an agent's claim about its own work. A reviewer's prose is all four. So the
reviewer answers only `clear|finding` per cell, code derives the verdict from
the rubric's severities, and the merge is gated on a **signed receipt** whose
signature the store re-verifies on load. The transition is caused by the
receipt, not by anything the reviewer said.

The named invariants this module carries, each a testable object because B15's
lesson is that an invariant which is only a field is an invariant a rewrite
silently drops:

* `require_located_findings`  — B8: FAIL is unrepresentable without at least
  one located blocking finding, and a finding without a message is not located.
* `ReviewHandoff`             — B9: the reviewer's input is a declared,
  validated contract, never an incidental assembly.
* `review_digest`             — B10: byte-identical bytes replay the stored
  verdict instead of being re-reviewed into a different answer.
* `require_distinct_vendor`   — B12: no actor reviews its own output.
* `preflight_handoff`         — B13: size-check the input against the
  reviewer's context window before dispatch, and fail closed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from pydantic import BaseModel, ConfigDict

from . import finalization as fin
from . import finalization_window as fw
from . import handoff_budget as hb
from . import scheduler_types as st


# ── the review subject: a diff, and the files it touches ────────────────────

#: A diff and each changed file are review objects in exactly the sense
#: `finalization.ObjectKind` already means. Extending that enum rather than
#: minting a parallel one is what lets `compute_matrix`, `verify_report`,
#: `derive_verdict`, `Receipt`, and `ReceiptStore` be reused verbatim.
DIFF = fin.ObjectKind.DIFF
CHANGED_FILE = fin.ObjectKind.CHANGED_FILE


#: The questions a diff is judged on. Severity is a property of the question
#: and is fixed here in code — the reviewer cannot express it, which is what
#: stops "I'll call this one advisory" from being a reviewer decision.
#:
#: Each BLOCKING check names a failure mode a green gate cannot see. That is
#: the selection rule: a question a passing test already answers does not
#: belong here, because it would spend a reviewer turn re-deriving something
#: mechanical, and §10.2's counting rule already owns it.
CODE_RUBRIC = fin.Rubric(
    version="maestro-code-rubric.v1",
    checks=(
        fin.RubricCheck(
            check_id="diff.implements_the_stated_instruction",
            question=("Does this diff do what the node was asked to do, rather "
                      "than something adjacent that happens to pass the gate?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.gate_is_passed_on_the_merits",
            question=("Does the code satisfy the gate because the behaviour is "
                      "present, rather than by hardcoding, special-casing the "
                      "test input, or weakening the assertion?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.no_unrelated_change_rides_along",
            question=("Is every hunk in service of this node's stated work, "
                      "with no unrelated refactor or drive-by edit?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.introduces_no_obvious_defect",
            question=("Is the changed code free of defects visible in the diff "
                      "— unhandled error paths, inverted conditions, resource "
                      "leaks, off-by-one, dead branches?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.is_coherent_with_its_surroundings",
            question=("Does the change fit the conventions and structure of the "
                      "code it lands in?"),
            applies_to=(DIFF,),
            severity=fin.Severity.ADVISORY),
        fin.RubricCheck(
            check_id="file.change_is_justified_by_the_instruction",
            question=("Is this file's change required by the node's stated work, "
                      "rather than incidental?"),
            applies_to=(CHANGED_FILE,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="file.no_secret_or_credential_introduced",
            question=("Does this file's change introduce no credential, token, "
                      "private key, or other secret?"),
            applies_to=(CHANGED_FILE,),
            severity=fin.Severity.BLOCKING),
    ))


# ── B8: FAIL is unrepresentable without a located finding ───────────────────

class VerdictNotLocated(RuntimeError):
    """B8's invariant, violated: a verdict whose findings do not support it.

    Strav shipped `VerdictV3` as `schema`/`status`/`summary` with no findings
    at all, so the reducer's scope guard had nothing to scope and a FAIL was a
    bare word. The field was retrofitted later; the *invariant* could not be,
    because a field added after the fact is optional forever. So this is
    enforced from v1, before any receipt for this subject exists.
    """


def require_located_findings(derived: fin.DerivedVerdict) -> None:
    """FAIL iff at least one graded cell carries a located blocking finding.

    Both directions are checked, and the second is the one that matters: a
    PASS carrying a blocking finding would mean the derivation and the cells
    disagree, which is exactly the contentless verdict B8 convicts, only
    inverted. "Located" is enforced too — a `finding` with an empty message
    names no place and no reason, so it cannot be handed back to a builder as
    retry guidance and does not count as a finding at all.
    """
    blocking: List[fin.DerivedCell] = []
    for cell in derived.cells:
        if cell.canary is not None:
            continue
        if cell.status is not fin.CellStatus.FINDING:
            continue
        if cell.severity is not fin.Severity.BLOCKING:
            continue
        if not cell.message.strip():
            raise VerdictNotLocated(
                f"blocking finding on {cell.check_id}/{cell.object_id} carries no "
                "message, so it locates nothing and cannot be acted on")
        blocking.append(cell)

    if derived.verdict is fin.Verdict.FAIL and not blocking:
        raise VerdictNotLocated(
            "FAIL without a located blocking finding is unrepresentable (B8)")
    if derived.verdict is fin.Verdict.PASS and blocking:
        raise VerdictNotLocated(
            "PASS while carrying {0} located blocking finding(s) (B8)".format(
                len(blocking)))


def blocking_findings(derived: fin.DerivedVerdict) -> Tuple[fin.DerivedCell, ...]:
    """The cells that caused a FAIL, in matrix order — the retry's content."""
    return tuple(
        cell for cell in derived.cells
        if cell.canary is None
        and cell.status is fin.CellStatus.FINDING
        and cell.severity is fin.Severity.BLOCKING)


def advisory_findings(derived: fin.DerivedVerdict) -> Tuple[fin.DerivedCell, ...]:
    """Non-blocking findings. Carried into the retry prompt but never a FAIL."""
    return tuple(
        cell for cell in derived.cells
        if cell.canary is None
        and cell.status is fin.CellStatus.FINDING
        and cell.severity is fin.Severity.ADVISORY)


# ── B12: no actor reviews its own output ────────────────────────────────────

class SelfJudgeRefused(RuntimeError):
    """B12: the reviewer and the builder are the same vendor."""


def require_distinct_vendor(builder_vendor: str, reviewer_vendor: str) -> None:
    """Refuse a review by the vendor that wrote the code.

    **This check is config-derived, not receipt-proven, and the difference is
    load-bearing.** `route_receipts` admits routes, and on the omp route only
    `profile` selects the model — `build_omp_argv` emits `--pm-profile` and
    `--session-dir` and passes neither model nor vendor. So a grok builder and
    an openai reviewer ride the *same* admitted `omp` route receipt, and no
    receipt anywhere can distinguish them. What is compared here is what the
    operator wrote in `maestro.config.yaml`, which an operator who edits both
    blocks to the same vendor can defeat.

    Closing it for real needs profile-scoped route receipts, so that the
    vendor a pane actually ran under is a signed fact rather than a
    configuration claim. That is a separate change. Until it lands this is a
    guard against the accident, not against the determined.
    """
    builder = (builder_vendor or "").strip().casefold()
    reviewer = (reviewer_vendor or "").strip().casefold()
    if not builder or not reviewer:
        raise SelfJudgeRefused(
            "both execution.vendor and reviewer.vendor must be configured; an "
            "unnamed vendor cannot be shown to differ from the builder's (B12)")
    if builder == reviewer:
        raise SelfJudgeRefused(
            f"reviewer vendor {reviewer_vendor!r} is the builder's; no actor may "
            "sign off on its own output (B12)")


# ── B13: size-check the handoff before dispatch, fail closed ────────────────

class HandoffTooLarge(RuntimeError):
    """B13: the reviewer's input would not fit its context window.

    An overflowing reviewer does not error — it compaction-loops and emits a
    plausible verdict about something else. Strav's round-two handoff was
    710,673 bytes against a 272K-context reviewer and produced a verdict
    describing a completely different workflow.
    """


#: B13's arithmetic and its route table live in `handoff_budget`, one module
#: below `launcher`, because the check that cannot be bypassed has to run
#: inside `launcher.HerdrLauncher.launch` and `launcher` cannot import this
#: module (`code_review` -> `scheduler_types` -> `worktree` -> `launcher`).
#: Re-exported here so the rule still reads as one rule from the reviewer's
#: side, with exactly one definition of it.
BYTES_PER_TOKEN = hb.BYTES_PER_TOKEN
HANDOFF_CONTEXT_FRACTION = hb.HANDOFF_CONTEXT_FRACTION
estimate_tokens = hb.estimate_tokens


def preflight_handoff(text: str, context_window_tokens: Optional[int],
                      fraction: float = HANDOFF_CONTEXT_FRACTION) -> int:
    """Refuse a handoff that will not fit. Returns the estimate when it does.

    Fails closed on an unknown window for the same reason
    `check_occupancy` convicts on NULL: an unmeasured window is not a
    passing one, and dispatching into it is how B13 happened.
    """
    if not context_window_tokens or context_window_tokens <= 0:
        raise HandoffTooLarge(
            "the reviewer's context window is unknown, so the handoff cannot be "
            "shown to fit; an unmeasured window is not a passing one (B13)")
    estimate = estimate_tokens(text)
    budget = hb.handoff_budget(context_window_tokens, fraction)
    if estimate > budget:
        raise HandoffTooLarge(
            f"handoff is ~{estimate} tokens against a {context_window_tokens}-token "
            f"window at fraction {fraction} (budget {budget}); an overflowing "
            "reviewer hallucinates rather than erroring (B13)")
    return estimate


#: Which routes publish a measurable window is a fact about the route, owned by
#: `handoff_budget` and re-exported here.
#:
#: The *lookup* deliberately lives at the CLI rather than in either module:
#: reading a route's catalog means importing `agent_pi`, and both are on the
#: far side of the `base-execution-import` boundary that `enforcement.py`
#: convicts. So the arithmetic and the rule are here, and the caller owns the
#: window.
ROUTES_PUBLISHING_A_WINDOW = hb.ROUTES_PUBLISHING_A_WINDOW
route_publishes_a_window = hb.route_publishes_a_window


# ── B9: the reviewer's input is a declared, validated contract ──────────────

class HandoffIncomplete(RuntimeError):
    """B9: a required part of the reviewer's contract is missing or empty."""


class ReviewHandoff(BaseModel):
    """Everything the reviewer is entitled to, declared rather than assembled.

    Strav's `review-golden_vendor` handoff was **409 bytes** — identifiers and
    a null. No goal, no `produces`, no acceptance contract. A reviewer given
    that cannot produce a meaningful verdict, and its PASS is worthless. So
    every field here is required and validated non-empty before a pane opens:
    a reviewer that would be starved is a launch that does not happen, which is
    a diagnosable refusal instead of a confident meaningless answer.

    `extra="forbid"` for the same reason the report schema forbids it — the
    contract is the whole contract, and a field smuggled in beside it is one
    nothing validates.
    """

    model_config = ConfigDict(extra="forbid")

    subject_digest: str
    run_id: str
    node_id: str
    node_kind: str
    #: The goal. B9's first missing field.
    instruction: str
    #: What the node was permitted to write. Without it "unrelated change"
    #: is not a question the reviewer can answer.
    declared_outputs: List[str]
    #: The acceptance contract. B9's third missing field.
    gate_command: List[str]
    gate_selector: str
    #: §10.2's counting threshold, which is half of what the gate demands.
    #: A gate over one new module passes on one test and on seven; the plan
    #: that asked for seven said so here and nowhere else, so a reviewer
    #: shown the command alone cannot tell a satisfied acceptance contract
    #: from a third of one.
    gate_min_cases: int = 1
    base_sha: str
    output_sha: str
    diff: str
    #: The cells to answer, and where to write the answers.
    matrix: List[Dict[str, Any]]
    pair_count: int
    report_path: str
    rubric: List[Dict[str, str]]

    def require_complete(self) -> "ReviewHandoff":
        """Every field carries content. Presence is not the check — the 409-byte
        handoff had every field it declared, and they were null and empty."""
        for name in ("subject_digest", "run_id", "node_id", "node_kind",
                     "instruction", "base_sha", "output_sha", "diff",
                     "report_path"):
            if not str(getattr(self, name) or "").strip():
                raise HandoffIncomplete(
                    f"the reviewer's contract has no {name}; a starved reviewer's "
                    "verdict is worthless (B9)")
        if not self.matrix:
            raise HandoffIncomplete(
                "the reviewer's contract names no cells to answer (B9)")
        if self.pair_count != len(self.matrix):
            raise HandoffIncomplete(
                f"pair_count {self.pair_count} does not match {len(self.matrix)} "
                "matrix cells; the fabrication canary must be internally consistent")
        if not self.rubric:
            raise HandoffIncomplete(
                "the reviewer's contract states no questions; a cell id without "
                "its question is not something a reviewer can answer (B9)")
        # A code node has no gate, so an empty gate contract is legal for it and
        # only for it — demanding one of every kind would refuse the composition
        # §6.7 recommends.
        if self.node_kind == st.NodeKind.AGENT.value and not self.gate_command:
            raise HandoffIncomplete(
                "an agent node's acceptance contract is its gate; the reviewer "
                "cannot judge 'passed on the merits' without it (B9)")
        return self

    def render(self) -> str:
        """The prompt text. Deterministic, so the same subject renders the same
        bytes and the size preflight measures what is actually sent."""
        lines = [
            "You are reviewing one node's diff before it merges. You did not "
            "write this code.",
            "",
            f"Run: {self.run_id}",
            f"Node: {self.node_id} (kind: {self.node_kind})",
            f"Base commit: {self.base_sha}",
            f"Proposed commit: {self.output_sha}",
            "",
            "## What this node was asked to do",
            self.instruction,
            "",
            "## Paths this node was permitted to write",
        ]
        lines.extend("  " + path for path in (self.declared_outputs or ["(none declared)"]))
        lines.append("")
        lines.append("## The acceptance contract it had to satisfy")
        if self.gate_command:
            lines.append("  " + " ".join(self.gate_command))
            lines.append(f"  selector: {self.gate_selector or '(none)'}")
            lines.append(
                f"  and it must collect at least {self.gate_min_cases} "
                "passing case(s)")
        else:
            lines.append("  (code node: acceptance is the command's exit code)")
        lines.extend([
            "",
            "## The diff, base..proposed",
            "```diff",
            self.diff,
            "```",
            "",
            "## The questions",
        ])
        for entry in self.rubric:
            lines.append(f"  {entry['check_id']}: {entry['question']}")
        lines.extend([
            "",
            "## What to write",
            "",
            f"Answer every one of these {self.pair_count} cells — exactly these, "
            "no more and no fewer:",
        ])
        for cell in self.matrix:
            lines.append(f"  {cell['check_id']} | {cell['object_id']}")
        lines.extend([
            "",
            "Two of those cells are controls whose object ids begin and end with "
            "a double underscore. Answer them the way the cell itself reads: the "
            "known-bad control is a finding, the known-good control is clear.",
            "",
            "Write this file and then stop:",
            "  " + self.report_path,
            "",
            "```json",
            json.dumps({
                "plan_digest": self.subject_digest,
                "pair_count": self.pair_count,
                "cells": [{"check_id": "<id>", "object_id": "<id>",
                           "status": "clear|finding",
                           "message": "<required when status is finding>"}],
            }, indent=2),
            "```",
            "",
            "`status` is the only judgment you make. Do not write a verdict, a "
            "severity, or a score — they are not fields, and a report carrying "
            "one is rejected unparsed. Every `finding` needs a message naming "
            "what is wrong and where; a finding without one is discarded and the "
            "review is refused.",
        ])
        return "\n".join(lines)


# ── B10: the subject digest, and what it deliberately excludes ──────────────

def review_digest(*, run_id: str, node_id: str, base_sha: str,
                  output_sha: str, rubric_version: str) -> str:
    """The identity of "this code, reviewed against this base, by this rubric".

    **`attempt_no` is deliberately not a component.** Including it would mint a
    fresh identity for a rebuilt-but-byte-identical output, which is exactly
    B10: FAIL then PASS on a byte-identical commit, the second review reaching
    the opposite conclusion and nothing recording that the intervening fix was
    empty. Excluding it means an empty resubmission hits the stored receipt and
    **replays the recorded verdict** rather than rolling the dice again — the
    guard is structural, not a comparison someone has to remember to run.

    `base_sha` *is* a component, because the same tree over a different base is
    genuinely different evidence: the surrounding code moved, so "coherent with
    its surroundings" and "no unrelated change" have different answers.

    `rubric_version` is a component so that changing the questions invalidates
    the cached answers rather than silently keeping them.
    """
    payload = json.dumps({
        "kind": "maestro-code-review.v1",
        "run_id": run_id,
        "node_id": node_id,
        "base_sha": base_sha,
        "output_sha": output_sha,
        "rubric_version": rubric_version,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ── the diff, and the objects it yields ─────────────────────────────────────

class DiffUnavailable(RuntimeError):
    """The subject of the review could not be read from git."""


#: A diff larger than this is truncated before the size preflight sees it, with
#: the truncation stated in the text. Refusing outright would block a
#: legitimately large node; sending it whole is B13.
MAX_DIFF_BYTES = 400_000


def read_diff(repo: Path, base_sha: str, output_sha: str,
              runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
              ) -> Tuple[str, Tuple[str, ...]]:
    """The unified diff `base..output`, and the paths it touches.

    Read from git rather than from the agent's envelope for the reason §1.2
    states: a `changed_files` list the agent wrote is the agent's claim about
    its own work, and a review scoped by it would review only what the author
    chose to disclose.
    """
    def git(*args: str) -> str:
        result = runner(["git", "-C", str(repo), *args],
                        capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DiffUnavailable(
                "git {0} -> {1}: {2}".format(" ".join(args), result.returncode,
                                             (result.stderr or "").strip()[-300:]))
        return result.stdout

    names = tuple(sorted(
        line.strip() for line in
        git("diff", "--name-only", f"{base_sha}..{output_sha}").splitlines()
        if line.strip()))
    text = git("diff", "--unified=3", f"{base_sha}..{output_sha}")
    if len(text.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES:
        text = (text.encode("utf-8", errors="replace")[:MAX_DIFF_BYTES]
                .decode("utf-8", errors="ignore")
                + "\n\n[diff truncated at {0} bytes]\n".format(MAX_DIFF_BYTES))
    return text, names


def review_objects(changed_files: Sequence[str], output_sha: str,
                   ) -> Tuple[fin.ReviewObject, ...]:
    """The diff itself, plus one object per changed file.

    Per-file objects are what make a finding *located* in B8's sense: a finding
    on `file.no_secret_or_credential_introduced` carries the path in its
    `object_id`, so the retry prompt can name the file rather than handing the
    builder "there is a secret somewhere".
    """
    objects = [fin.ReviewObject(object_id=f"diff:{output_sha}", kind=DIFF)]
    objects.extend(
        fin.ReviewObject(object_id=f"file:{path}", kind=CHANGED_FILE)
        for path in changed_files)
    return tuple(objects)


# ── the outcome the scheduler acts on ───────────────────────────────────────

@dataclass(frozen=True)
class ReviewOutcome:
    """One review, as one reportable fact."""

    subject_digest: str
    verdict: fin.Verdict
    receipt: fin.Receipt
    replayed: bool
    findings: Tuple[fin.DerivedCell, ...] = ()
    advisories: Tuple[fin.DerivedCell, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict is fin.Verdict.PASS

    def findings_text(self) -> str:
        """The findings as retry guidance, blocking first.

        This is the whole justification for recycling the attempt rather than
        blocking it: a retry that repeats the same request is not new
        instructions. The builder is told which cell failed, on which object,
        and why — the same shape §7.5 requires of a SEMANTIC retry.
        """
        lines: List[str] = []
        if self.findings:
            lines.append("Blocking findings — each must be resolved:")
            for cell in self.findings:
                lines.append(f"  [{cell.object_id}] {cell.check_id}")
                lines.append(f"    {cell.message.strip()}")
        if self.advisories:
            lines.append("Advisory findings — address if you agree:")
            for cell in self.advisories:
                lines.append(f"  [{cell.object_id}] {cell.check_id}")
                lines.append(f"    {cell.message.strip()}")
        return "\n".join(lines)


class ReviewStalled(RuntimeError):
    """The reviewer never produced a report inside its window.

    Not a FAIL, and deliberately not a receipt: a stall is a fact about the
    machine or the route, never a verdict about the code. It carries no
    review-rejection marker either, so it spends no review budget — a wedged
    herdr must not consume a node's three attempts.
    """

    def __init__(self, session: fw.ReviewerSession,
                 signal: fw.FinalizationSignal, elapsed_s: float) -> None:
        super().__init__(
            f"REVIEW_STALLED: route={session.route} model={session.model} "
            f"session_id={session.session_id} signal={signal.value} "
            f"after {elapsed_s:.1f}s")
        self.route = session.route
        self.model = session.model
        self.session_id = session.session_id
        self.signal = signal
        self.elapsed_s = elapsed_s


# ── the driver ──────────────────────────────────────────────────────────────

def review_attempt(
    *,
    subject_digest: str,
    handoff: ReviewHandoff,
    objects: Sequence[fin.ReviewObject],
    rubric: fin.Rubric,
    store: fin.ReceiptStore,
    window_factory: Callable[[fin.ApplicabilityMatrix], Any],
    occupancy_reader: Callable[[fw.ReviewerSession], Optional[float]],
    occupancy_threshold: float = 0.8,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> ReviewOutcome:
    """One code review, in the order `finalization.finalize` fixes.

    The order is the same and for the same reasons: replay before any reviewer
    call, every rejection before any receipt exists, the verdict derived in
    code, the receipt create-once and signed. What differs is the subject and
    two added obligations — B9's contract completeness, checked before a pane
    opens, and B8's located-findings invariant, checked before the receipt is
    written.
    """
    handoff.require_complete()
    store.recover(subject_digest)

    # B10. A byte-identical resubmission replays the recorded verdict, and a
    # replayed FAIL still recycles the attempt — it just does so without
    # spending a reviewer turn to re-derive an answer already on disk.
    if store.has(subject_digest):
        receipt = store.load(subject_digest)
        derived = fin.DerivedVerdict(verdict=receipt.verdict, cells=receipt.cells)
        return ReviewOutcome(
            subject_digest=subject_digest, verdict=receipt.verdict,
            receipt=receipt, replayed=True,
            findings=blocking_findings(derived),
            advisories=advisory_findings(derived))

    matrix = fin.compute_matrix(rubric, subject_digest, objects)
    window = window_factory(matrix)
    outcome: fw.WindowOutcome = window.run(sleep=sleep)
    if not outcome.completed:
        if outcome.signal is None:
            raise RuntimeError(
                "REVIEW_PROTOCOL_ERROR: incomplete window without signal")
        raise ReviewStalled(outcome.session, outcome.signal, outcome.elapsed_s)

    report = fin.ReviewerReport.model_validate(outcome.report)
    fin.check_occupancy(occupancy_reader(outcome.session),
                        threshold=occupancy_threshold)
    fin.verify_report(matrix, report)

    derived = fin.derive_verdict(matrix, report, rubric)
    require_located_findings(derived)

    created_at_epoch = clock()
    receipt = fin.Receipt(
        plan_digest=subject_digest,
        rubric_version=rubric.version,
        verdict=derived.verdict,
        cells=derived.cells,
        reviewer=fin.ReviewerIdentity(route=outcome.session.route,
                                      model=outcome.session.model,
                                      session_id=outcome.session.session_id),
        created_at_epoch=created_at_epoch)
    replayed = False
    try:
        store.write(receipt)
    except fin.ReceiptExists:
        receipt = store.load(subject_digest)
        derived = fin.DerivedVerdict(verdict=receipt.verdict, cells=receipt.cells)
        replayed = True
    return ReviewOutcome(
        subject_digest=subject_digest, verdict=receipt.verdict, receipt=receipt,
        replayed=replayed, findings=blocking_findings(derived),
        advisories=advisory_findings(derived))


def build_handoff(
    *,
    subject_digest: str,
    run_id: str,
    node: st.PlanNode,
    base_sha: str,
    output_sha: str,
    diff: str,
    matrix: fin.ApplicabilityMatrix,
    rubric: fin.Rubric,
    report_path: Path,
) -> ReviewHandoff:
    """Assemble and validate the reviewer's contract (B9)."""
    asked = {cell.check_id for cell in matrix.cells}
    return ReviewHandoff(
        subject_digest=subject_digest,
        run_id=run_id,
        node_id=node.node_id,
        node_kind=node.kind.value,
        instruction=_node_goal(node),
        declared_outputs=list(node.outputs),
        gate_command=list(node.gate_command),
        gate_selector=node.gate_selector or "",
        gate_min_cases=node.gate_min_cases,
        base_sha=base_sha,
        output_sha=output_sha,
        diff=diff,
        matrix=[{"check_id": cell.check_id, "object_id": cell.object_id}
                for cell in matrix.cells],
        pair_count=matrix.pair_count,
        report_path=str(report_path.resolve()),
        rubric=[{"check_id": check.check_id, "question": check.question}
                for check in rubric.checks if check.check_id in asked]
        + [{"check_id": fin.CANARY_CHECK_ID,
            "question": ("Control cell. Answer it as the object id states: the "
                         "known-bad object is a finding, the known-good object "
                         "is clear.")}],
    ).require_complete()


class InstructionNotCarried(HandoffIncomplete):
    """An agent node reached the reviewer with no instruction on it.

    Kept as its own class because the two ways a goal can be missing are not
    the same failure and must not be handled the same way. A code node has no
    instruction to carry — its goal *is* its command (§6.2), and deriving one
    from that command is reading the contract, not guessing at it. An agent
    node's instruction is `min_length=1` in the plan model, so a blank one
    cannot be a plan that omitted a goal; it can only be a projection that
    dropped one, and that is exactly the state the system shipped in.

    So this is a refusal rather than a fallback. A fallback here would rebuild
    the defect it is meant to catch: the derivable goal is "make your own gate
    pass", which is not independent of the thing the reviewer is judging, and
    it is indistinguishable in the prompt from a real instruction that happens
    to be terse. Refusing fails closed at the one place that can still tell
    the difference — before a reviewer pane opens, with the node named.
    """


def _node_goal(node: st.PlanNode) -> str:
    """The goal the plan declared for this node, by node kind (§1.1 item 4).

    An agent node's goal is carried; a code node's goal is its command, which
    is what a code node declares in place of an instruction.
    """
    if node.kind is st.NodeKind.CODE:
        return ("Run the command {0!r} and change only the declared outputs."
                .format(" ".join(node.command)))
    if not node.instruction.strip():
        raise InstructionNotCarried(
            "{0}: the reviewer's contract has no instruction. An agent node's "
            "instruction is required and non-empty in the plan, so this is a "
            "projection that dropped it, not a plan that omitted it. Deriving "
            "one from the node's own gate is what made every agent node in "
            "every run reviewable only against 'make this command pass' "
            "(B9).".format(node.node_id))
    return node.instruction
