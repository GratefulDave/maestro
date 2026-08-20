"""Findings-per-attempt convergence, rebuilt from the lifecycle ledger (#30).

`execution.review_ceiling` was raised from 3 to 6 by hand, from three lanes of
one run read out of a terminal. Nothing in the runtime could answer the
question that decision needed answering — *was the reviewer finding less each
attempt, and how many attempts did a lane that eventually passed actually
take* — once the scheduler process exited. `SchedulerReport.review_convergence`
answers it for the process that finished the run and for nobody else: a run
that was resumed, cancelled, or read the next morning has no surface at all.

This module is that surface, and it derives every number from typed lifecycle
records:

* **that a reviewer rejected an attempt** — `attempts.extra_json`'s
  `review_rejected` marker, the same stored fact `review_ceiling` is counted
  over (`retry_policy.review_attempts_total`);
* **how many findings it raised** — `review_findings_count` on that same row
  when present, and otherwise the length of the `blocking_checks` list on the
  node's `retry:SEMANTIC` / `blocked:REVIEW_BUDGET_EXHAUSTED` transition,
  joined to the attempt by `review_subject_digest`. The scheduler writes both
  from one list in one transaction — `len(review.findings)` and
  `[c.check_id for c in review.findings]` — so the two are equal by
  construction, and the fallback is what makes a run recorded before the count
  key existed readable rather than blank;
* **that an attempt cleared the review gate** — `attempts.state == VERIFIED`,
  which the scheduler writes in `mark_verified`, *after* the review gate and
  before the merge frontier can see the node.

**What a count counts.** `ReviewOutcome.findings` is the findings *at or above
the reject threshold* — the ones that refused the merge. Advisories and
scope-unreachable findings are recorded separately and are not in it, so this
series is blocking findings per attempt and is smaller than the number of
findings a reviewer's pane showed. The issue that asked for this reports
`lane-p1-canonical-object-key` as `5, 5, 3, 1`; the ledger for that same lane
in `run-9e9ac412669140039ae078601048f6c7` says `a2:3 a3:1`, and the difference
is exactly this — non-blocking findings, and an attempt that failed its gate
before any reviewer saw it. The stored number is the one `review_ceiling` is
actually spent on, which is the number the ceiling has to be sized against.

None of those is pane text, prompt text, a free-text envelope field, or an
agent's claim about its own work (§1.2). `blocking_checks` holds check ids
from a closed vocabulary, never a reviewer's prose: the finding *messages* are
deliberately not in the ledger, and this module neither wants nor reads them.
Nothing here decides a lifecycle transition either — it is a read verb, so the
prohibition it must respect is the weaker one about what a *report* may claim,
and it claims only what a stored row says.

**Why VERIFIED, and not MERGED.** MERGED is the obvious convergence witness
and it is the wrong one. `skip --accept-sha` takes a node from BLOCKED to
MERGED on an operator's signature, so a lane that exhausted its review budget
and was merged over the reviewer's objection reads as MERGED with no review
pass anywhere in its history. Run `run-2a44d226e75a4be391a14f02b78a6d25` has
exactly that shape: `lane-p4-enrichment-ordering` collected seven rejections,
was blocked twice, and reached MERGED by operator skip. Counting it as a lane
that converged in eleven attempts would feed the ceiling the one number it
must never be sized from — the length of a lane that never converged at all.

**A run that has not finished has not failed to converge (#107).** The verdict
column had one residual cause for every lane that was rejected and never
passed, and its text was "run ended first". Read against a live run it was
simply false: `run convergence` printed "not converged — run ended first;
still descending" for `lane-p5-gap-policy` while that lane was RUNNING on its
fourth attempt, `runs.latest_outcome` was NULL, and the scheduler process was
alive — and the summary line closed with "no lane in this run converged, so it
measures no convergence length", a terminal judgement on a run that could
still converge minutes later.

The measurement was never wrong; `a1:2 a2:2 a3:1` was exactly the ledger, and
the trend beside it was the signal the operator was actually watching. What
was wrong was that one verdict covered two different facts. Whether the run
has stopped is `lifecycle.run_in_flight` — `derive_run_state` over the node
rows, composed with `scheduler_liveness` over the process table, and `None`
where neither can answer — and it arrives here as a value the caller computed
from the `runs` row, never as anything a lane says about itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Tuple)

from . import retry_policy as rp

#: The digest the scheduler stamps on both the attempt row and the transition
#: it writes in the same transaction. It is the join key between the two, and
#: it is a digest rather than an ordinal so a replayed review, an interleaved
#: sibling, or a resumed process cannot pair a count with the wrong attempt.
#: Spelled here rather than imported because the scheduler writes it as a
#: literal; a reader that guessed at pairing by position would be inventing
#: the very association this key already records.
REVIEW_SUBJECT_DIGEST_KEY = "review_subject_digest"

#: The transition reasons that carry a rejected review's `blocking_checks`.
#: Both are written by the same `_settle_review_rejection` call — the first
#: when the node earns another attempt, the second when that rejection was the
#: one that spent the last of `review_ceiling`.
REVIEW_TRANSITION_REASONS = ("retry:SEMANTIC", "blocked:REVIEW_BUDGET_EXHAUSTED")

#: The attempt state `mark_verified` writes. Named as a literal for the same
#: reason the digest key is: this module reads the ledger, and the ledger
#: stores strings.
VERIFIED_ATTEMPT_STATE = "VERIFIED"

MERGED_NODE_STATE = "MERGED"
REVIEW_BUDGET_EXHAUSTED = "REVIEW_BUDGET_EXHAUSTED"


class Outcome(str, Enum):
    """What the ledger says happened to this lane's review loop."""

    #: A rejected attempt was followed by one that cleared the review gate.
    CONVERGED = "CONVERGED"
    #: The reviewer rejected at least once and no later attempt ever passed.
    NOT_CONVERGED = "NOT_CONVERGED"
    #: No attempt of this lane is recorded as rejected *or* as verified, so
    #: the ledger cannot say the lane ever reached a reviewer. Distinct from
    #: `CONVERGED` with an empty series, which is a lane that passed review
    #: the first time it was asked.
    NO_REVIEW = "NO_REVIEW"


class Cause(str, Enum):
    """Why a `NOT_CONVERGED` lane stopped, as the ledger records it."""

    #: The node is blocked REVIEW_BUDGET_EXHAUSTED — `review_ceiling` cut the
    #: loop off, and the series says whether it was still descending.
    REVIEW_CEILING_REACHED = "REVIEW_CEILING_REACHED"
    #: The node reached MERGED without any attempt in VERIFIED: an operator
    #: `skip` merged it over the reviewer. Its attempt count is not a
    #: convergence length and must not be read as one.
    MERGED_WITHOUT_PASSING_REVIEW = "MERGED_WITHOUT_PASSING_REVIEW"
    #: The run stopped — merged, cancelled, quiescent, or abandoned — before a
    #: later attempt could pass. This is a finding about the plan: the lane had
    #: its chance and did not take it, and the series says whether it was still
    #: descending when the run went away.
    RUN_ENDED = "RUN_ENDED"
    #: The run has *not* stopped. Nothing has been decided about this lane yet;
    #: it simply has not finished. Distinct from `RUN_ENDED` because the
    #: two are different facts and only the first is a finding about the plan —
    #: single verdict that used to cover both told an operator watching a live
    #: run that it had already ended (#107).
    RUN_IN_FLIGHT = "RUN_IN_FLIGHT"
    #: Whether the run is still going cannot be established: no scheduler pid
    #: is recorded, or it was recorded on a host whose process table this
    #: machine cannot read (`lifecycle.run_in_flight` answers `None`). The
    #: series is still exactly what the ledger holds; the *reason* the lane
    #: stopped being retried is what is unknown, and reporting it as ended
    #: would be the same overclaim in the other direction.
    RUN_LIVENESS_UNKNOWN = "RUN_LIVENESS_UNKNOWN"


@dataclass(frozen=True)
class LaneConvergence:
    """One lane's review loop, as the durable rows record it."""

    node_id: str
    state: str
    block_reason: Optional[str]
    granted_extra_attempts: int
    #: `(attempt_no, findings)` per review-rejected attempt, in attempt order.
    #: `findings` is `None` for a row written before `review_findings_count`
    #: existed whose transition could not be joined — "the reviewer rejected
    #: and the count is unrecoverable", never zero, because zero findings is
    #: the one thing a rejection cannot mean.
    findings_per_attempt: Tuple[Tuple[int, Optional[int]], ...]
    #: The attempt that cleared the review gate, if any.
    passed_at_attempt: Optional[int]
    outcome: Outcome
    cause: Optional[Cause]

    @property
    def rejections(self) -> int:
        """How many review-rejected attempts this lane spent."""
        return len(self.findings_per_attempt)

    @property
    def convergence_length(self) -> Optional[int]:
        """Review-reaching attempts up to and including the one that passed.

        `None` unless the lane converged, because for every other outcome the
        rejection count is a floor the run happened to stop at and not a
        length. Sizing a ceiling from a lane that was cut off is how the
        ceiling stays exactly as large as it already was.
        """
        if self.outcome is not Outcome.CONVERGED:
            return None
        return self.rejections + 1

    @property
    def descending(self) -> Optional[bool]:
        """Whether the last recorded count is below the first.

        A flat series is a lane more attempts would not have saved; a
        descending one that was cut off is a lane the ceiling stopped early.
        `None` when fewer than two counts survived, because a trend needs two
        points and asserting one from a single count would be the anecdote
        this module exists to replace.
        """
        counts = [count for _no, count in self.findings_per_attempt
                  if count is not None]
        if len(counts) < 2:
            return None
        return counts[-1] < counts[0]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "state": self.state,
            "block_reason": self.block_reason,
            "granted_extra_attempts": self.granted_extra_attempts,
            "findings_per_attempt": [
                {"attempt_no": attempt_no, "findings": findings}
                for attempt_no, findings in self.findings_per_attempt],
            "rejections": self.rejections,
            "passed_at_attempt": self.passed_at_attempt,
            "convergence_length": self.convergence_length,
            "descending": self.descending,
            "outcome": self.outcome.value,
            "cause": self.cause.value if self.cause else None,
        }


@dataclass(frozen=True)
class RunConvergence:
    """Every lane of one run, plus the ceiling the run was configured with."""

    run_id: str
    review_ceiling: Optional[int]
    lanes: Tuple[LaneConvergence, ...]
    #: Whether the run had stopped when this profile was taken, as
    #: `lifecycle.run_in_flight` derives it — `True` still going, `False`
    #: stopped, `None` not establishable. Carried on the profile rather than
    #: re-derived per lane so every lane of one reading answers from one
    #: observation of one ledger; a run that ends between two lanes must not
    #: produce a profile that disagrees with itself.
    in_flight: Optional[bool] = None

    @property
    def converged(self) -> Tuple[LaneConvergence, ...]:
        return tuple(lane for lane in self.lanes
                     if lane.outcome is Outcome.CONVERGED)

    @property
    def longest(self) -> Optional[LaneConvergence]:
        """The converged lane that needed the most review attempts.

        Ties break on `node_id` so the same run always names the same lane —
        an operator comparing two readings of one ledger must not see the
        answer move.
        """
        ranked = sorted(
            self.converged,
            key=lambda lane: (-(lane.convergence_length or 0), lane.node_id))
        return ranked[0] if ranked else None

    @property
    def ceiling_warning(self) -> Optional[str]:
        """Set when the configured ceiling is below an observed convergence.

        The comparison is `review_ceiling < convergence_length`, and the
        strictness is the whole point. `_review_ceiling_reached` blocks a node
        when its rejections *reach* `review_ceiling + granted`, so a lane that
        needed `n` review attempts — `n - 1` rejections and then a pass —
        survives only while `review_ceiling + granted > n - 1`. A ceiling
        equal to `n` is the smallest one that would have let the lane land
        unaided; anything below it means the lane converged only because an
        operator granted it an attempt the configuration did not.

        Deliberately not a recommendation. It reports that the run itself
        contradicted its configuration and names the lane that did it; what
        the ceiling should become stays an operator's decision on evidence,
        which is the whole distinction between this and deriving the value.
        """
        longest = self.longest
        if longest is None or self.review_ceiling is None:
            return None
        length = longest.convergence_length or 0
        if self.review_ceiling >= length:
            return None
        return (
            "execution.review_ceiling is {ceiling}, and {node} converged only "
            "after {length} review attempts in this run; {ceiling} would have "
            "blocked it".format(ceiling=self.review_ceiling,
                                node=longest.node_id, length=length))

    def as_dict(self) -> Dict[str, Any]:
        longest = self.longest
        return {
            "run_id": self.run_id,
            "review_ceiling": self.review_ceiling,
            "in_flight": self.in_flight,
            "longest_convergence": (
                {"node_id": longest.node_id,
                 "convergence_length": longest.convergence_length}
                if longest is not None else None),
            "ceiling_warning": self.ceiling_warning,
            "lanes": [lane.as_dict() for lane in self.lanes],
        }


# ── derivation ───────────────────────────────────────────────────────────────

def _extra(attempt: Any) -> Mapping[str, Any]:
    return getattr(attempt, "extra", None) or {}


def _name(value: Any) -> Optional[str]:
    """An enum member, a bare string, or nothing — as the stored string."""
    if value is None:
        return None
    return getattr(value, "value", value)


def findings_by_subject(node_id: str,
                        transitions: Iterable[Mapping[str, Any]]
                        ) -> Dict[str, int]:
    """`subject_digest -> findings count`, from this node's review transitions.

    The count is `len(detail["blocking_checks"])`. That list is the check ids
    of the same `review.findings` the scheduler counted into
    `review_findings_count`, written in the same transaction from the same
    list, so its length is that count and not an approximation of it. The
    messages are absent from the ledger by design and nothing here needs them.
    """
    counts: Dict[str, int] = {}
    for row in transitions:
        if row.get("node_id") != node_id:
            continue
        if row.get("reason") not in REVIEW_TRANSITION_REASONS:
            continue
        detail = row.get("detail") or {}
        digest = detail.get("subject_digest")
        checks = detail.get("blocking_checks")
        if not isinstance(digest, str) or not isinstance(checks, (list, tuple)):
            continue
        counts.setdefault(digest, len(checks))
    return counts


def findings_per_attempt(node_id: str, attempts: Iterable[Any],
                         transitions: Iterable[Mapping[str, Any]]
                         ) -> Tuple[Tuple[int, Optional[int]], ...]:
    """The findings-per-attempt series for one lane, in attempt order.

    Ordered by `attempt_no` rather than by insertion, so a resumed run and the
    process that started it report the same series — the same reason
    `retry_policy.review_convergence_from_attempts` sorts.
    """
    by_digest = findings_by_subject(node_id, transitions)
    series: List[Tuple[int, Optional[int]]] = []
    for attempt in attempts:
        if getattr(attempt, "node_id", None) != node_id:
            continue
        extra = _extra(attempt)
        if not extra.get(rp.REVIEW_REJECTED_KEY):
            continue
        count = extra.get(rp.REVIEW_FINDINGS_COUNT_KEY)
        if count is None:
            count = by_digest.get(extra.get(REVIEW_SUBJECT_DIGEST_KEY))
        series.append((attempt.attempt_no,
                       None if count is None else int(count)))
    # Keyed on the attempt number alone. Sorting the pairs would compare the
    # counts on a tie, and an unrecoverable count is `None` — which no
    # attempt number can currently collide with, and which would raise rather
    # than mis-order if one ever did.
    return tuple(sorted(series, key=lambda item: item[0]))


def _passed_at(node_id: str, attempts: Iterable[Any]) -> Optional[int]:
    """The highest attempt of this node whose row reads VERIFIED.

    Highest rather than first because a node reopened by `operator-reopen`
    runs again from a VERIFIED history, and the review loop the operator is
    sizing a ceiling from is the one that just ran.
    """
    verified = [attempt.attempt_no for attempt in attempts
                if getattr(attempt, "node_id", None) == node_id
                and _name(getattr(attempt, "state", None))
                == VERIFIED_ATTEMPT_STATE]
    return max(verified) if verified else None


def _unfinished_cause(in_flight: Optional[bool]) -> Cause:
    """Why a still-descending lane stopped being retried — or that it has not.

    Nothing here is a judgement about the lane. The lane's own rows have
    already been exhausted by the two branches above (the ceiling blocked it,
    or an operator merged over the reviewer); what is left is entirely a
    question about the *run*, and the answer is `lifecycle.run_in_flight`'s
    tri-state passed straight through. The mapping is total and the `None` arm
    is deliberate: a reader that cannot say must say that, not pick the
    likelier of the two claims it is unable to support.
    """
    if in_flight is True:
        return Cause.RUN_IN_FLIGHT
    if in_flight is False:
        return Cause.RUN_ENDED
    return Cause.RUN_LIVENESS_UNKNOWN


def lane_convergence(node: Any, attempts: Iterable[Any],
                     transitions: Iterable[Mapping[str, Any]],
                     in_flight: Optional[bool] = None) -> LaneConvergence:
    """One lane's profile, from the rows the ledger already holds.

    `in_flight` is the run's, not the lane's, and it changes no measurement:
    the series, the pass, and the convergence length are the same rows either
    way. It selects which of three true statements the residual verdict makes.
    """
    attempts = tuple(attempts)
    transitions = tuple(transitions)
    node_id = node.node_id
    series = findings_per_attempt(node_id, attempts, transitions)
    passed_at = _passed_at(node_id, attempts)
    state = _name(getattr(node, "state", None)) or ""
    block_reason = _name(getattr(node, "block_reason", None))

    converged = passed_at is not None and (
        not series or passed_at > series[-1][0])
    if converged:
        outcome, cause = Outcome.CONVERGED, None
    elif series:
        outcome = Outcome.NOT_CONVERGED
        if block_reason == REVIEW_BUDGET_EXHAUSTED:
            cause = Cause.REVIEW_CEILING_REACHED
        elif state == MERGED_NODE_STATE:
            cause = Cause.MERGED_WITHOUT_PASSING_REVIEW
        else:
            cause = _unfinished_cause(in_flight)
    else:
        outcome, cause = Outcome.NO_REVIEW, None

    return LaneConvergence(
        node_id=node_id, state=state, block_reason=block_reason,
        granted_extra_attempts=int(
            getattr(node, "granted_extra_attempts", 0) or 0),
        findings_per_attempt=series, passed_at_attempt=passed_at,
        outcome=outcome, cause=cause)


def run_convergence(run_id: str, nodes: Iterable[Any],
                    attempts: Iterable[Any],
                    transitions: Iterable[Mapping[str, Any]],
                    review_ceiling: Optional[int] = None,
                    in_flight: Optional[bool] = None) -> RunConvergence:
    """Every lane of one run, ordered as the reader returned the nodes.

    `in_flight` comes from `lifecycle.run_in_flight`, computed once by the
    caller that already holds the `runs` row, and is handed to every lane
    unchanged. This module stays duck-typed over nodes, attempts, and
    transitions — it has never held a `RunRecord` and does not start now.
    """
    attempts = tuple(attempts)
    transitions = tuple(transitions)
    return RunConvergence(
        run_id=run_id, review_ceiling=review_ceiling, in_flight=in_flight,
        lanes=tuple(lane_convergence(node, attempts, transitions, in_flight)
                    for node in nodes))


# ── rendering ────────────────────────────────────────────────────────────────

_OUTCOME_TEXT = {
    Outcome.CONVERGED: "converged",
    Outcome.NOT_CONVERGED: "not converged",
    Outcome.NO_REVIEW: "no review-reaching attempt",
}

_CAUSE_TEXT = {
    Cause.REVIEW_CEILING_REACHED: "review ceiling reached",
    Cause.MERGED_WITHOUT_PASSING_REVIEW: "merged without passing review",
    Cause.RUN_ENDED: "run ended first",
    Cause.RUN_IN_FLIGHT: "run still in flight",
    Cause.RUN_LIVENESS_UNKNOWN: "no later attempt recorded",
}

#: `not converged` is a verdict; on a run that has not finished there is no
#: verdict yet, and the word that carries the whole difference is "yet". The
#: line an operator saw read "not converged — run ended first" beside a lane
#: that was RUNNING on its fourth attempt at that moment (#107).
_IN_FLIGHT_OUTCOME_TEXT = "not converged yet"


def _series_text(lane: LaneConvergence) -> str:
    if not lane.findings_per_attempt:
        return "-"
    return " ".join(
        "a{}:{}".format(attempt_no, "?" if count is None else count)
        for attempt_no, count in lane.findings_per_attempt)


def _attempts_text(count: Optional[int]) -> str:
    return "{} review attempt{}".format(count, "" if count == 1 else "s")


def _verdict_text(lane: LaneConvergence) -> str:
    text = (_IN_FLIGHT_OUTCOME_TEXT if lane.cause is Cause.RUN_IN_FLIGHT
            else _OUTCOME_TEXT[lane.outcome])
    if lane.outcome is Outcome.CONVERGED:
        text += " at a{} ({})".format(
            lane.passed_at_attempt, _attempts_text(lane.convergence_length))
    elif lane.cause is not None:
        text += " — " + _CAUSE_TEXT[lane.cause]
    if lane.descending is True and lane.outcome is Outcome.NOT_CONVERGED:
        # The distinction the ceiling turns on: a lane cut off while the
        # reviewer was still finding less each time is one more attempt would
        # plausibly have landed, and a flat one is not.
        text += "; still descending"
    elif lane.descending is False:
        text += "; findings flat or rising"
    if lane.granted_extra_attempts:
        text += "; +{} granted".format(lane.granted_extra_attempts)
    return text


def render(profile: RunConvergence) -> str:
    """The operator's view: one line per lane, then what it means for the ceiling."""
    lines = ["run {}".format(profile.run_id)]
    lines.append("  review_ceiling {}".format(
        profile.review_ceiling if profile.review_ceiling is not None
        else "(not configured for this invocation)"))
    lines.append("")
    lines.append("  {:<44} {:<40} {}".format(
        "LANE", "FINDINGS/ATTEMPT", "CONVERGENCE"))
    for lane in profile.lanes:
        # The series column is padded and never truncated. A long series is
        # exactly the lane an operator is reading this for, and clipping it
        # would hide the evidence at the one row that carries it.
        lines.append("  {:<44} {:<40} {}".format(
            lane.node_id[:44], _series_text(lane), _verdict_text(lane)))
    lines.append("")
    longest = profile.longest
    if longest is None:
        # Three sentences for three states, and the tense is the whole point.
        # "converged" is past and closed; a run still executing has not
        # finished failing to converge, and saying so read as a terminal
        # judgement on a run that could still converge minutes later (#107).
        if profile.in_flight is True:
            lines.append("  no lane has converged yet and the run has not "
                         "ended, so it measures no convergence length yet")
        elif profile.in_flight is False:
            lines.append("  no lane in this run converged, so it measures no "
                         "convergence length")
        else:
            lines.append("  no lane in this run has converged, so it measures "
                         "no convergence length")
    else:
        lines.append("  longest observed convergence: {} ({})".format(
            _attempts_text(longest.convergence_length), longest.node_id))
    warning = profile.ceiling_warning
    if warning:
        lines.append("  WARNING: " + warning)
    return "\n".join(lines)
