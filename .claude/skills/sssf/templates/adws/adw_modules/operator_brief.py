"""A projection of typed records into prose an operator can act on.

A lane that parks `WAITING_FOR_USER` gives the operator one log line and a
status JSON, neither of which says whether the next verb is `run resume` or
`run amend`. This module renders the records that already decided the park --
the `USER_WAIT` artifact, the lane's review history, the findings each review
carried -- as a few sentences.

Three properties hold and are load-bearing:

* It is a pure read. It opens no transaction, records no artifact, and causes
  no transition. What it says is never workflow authority; the authority is
  `lane_state.stage` and the immutable artifacts it reads.
* It quotes only the public, redacted half of a finding -- `violated_requirement`
  and `implementation_area`. The vault, private results, and private source are
  never read here.
* It derives every claim from a stored field. It never asks an agent, never
  reads pane text, and never restates an agent's prose about its own work.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from . import scheduler_types as st

#: The operator surface is frozen at four verbs; these are two of them.
RESUME_COMMAND = "uv run adws/maestro.py run resume {run_id}"
AMEND_COMMAND = "uv run adws/maestro.py run amend <plan> --run {run_id}"

#: How many reviews back the brief reads. Older rounds are summarised by
#: nothing at all: a history longer than this says the same thing louder.
REVIEW_WINDOW = 5

CONVERGING = "converging"
REPEATING = "repeating"


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split())


def _finding_key(finding: Mapping[str, Any]) -> tuple[str, str]:
    """The two public fields that identify a finding across rounds."""
    return (
        _norm(finding.get("implementation_area")),
        _norm(finding.get("violated_requirement")),
    )


def _finding_set(payload: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset(_finding_key(item) for item in payload.get("findings") or ())


def _outcome_phrase(payload: Mapping[str, Any]) -> str:
    """One round's measured result, from `public_result_summary` alone."""
    summary = payload.get("public_result_summary") or {}
    if not isinstance(summary, Mapping) or not summary:
        return "no measured suite"
    parts = []
    for key in ("failed", "errored"):
        count = summary.get(key)
        if isinstance(count, int) and count:
            parts.append("{0} {1}".format(count, key))
    if parts:
        return ", ".join(parts)
    executed = summary.get("executed")
    passed = summary.get("passed")
    if isinstance(executed, int) and isinstance(passed, int):
        return "{0}/{1} passed".format(passed, executed)
    collected = summary.get("collected")
    if isinstance(collected, int):
        return "{0} collected".format(collected)
    return "no measured suite"


def _rounds_line(records: Sequence[Mapping[str, Any]]) -> str:
    """`reviews 11,13: 2 failed; 15,17,19: 1 failed; verdict REVISE each round`."""
    groups: list[tuple[list[int], str]] = []
    for record in records:
        phrase = _outcome_phrase(record["payload"])
        if groups and groups[-1][1] == phrase:
            groups[-1][0].append(int(record["sequence"]))
        else:
            groups.append(([int(record["sequence"])], phrase))
    body = "; ".join(
        "{0}: {1}".format(",".join(str(seq) for seq in seqs), phrase)
        for seqs, phrase in groups
    )
    verdicts = [str(record["payload"].get("verdict") or "?") for record in records]
    if len(set(verdicts)) == 1:
        tail = "verdict {0} each round".format(verdicts[0])
    else:
        tail = "verdicts " + ",".join(verdicts)
    return "Reviews {0}; {1}.".format(body, tail)


def _classify(records: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """`repeating` if any round repeats its predecessor's findings verbatim."""
    if len(records) < 2:
        return None
    sets = [_finding_set(record["payload"]) for record in records]
    for earlier, later in zip(sets, sets[1:]):
        if earlier == later:
            return REPEATING
    return CONVERGING


def _tail_repeats(records: Sequence[Mapping[str, Any]]) -> int:
    """How many consecutive latest rounds carry the latest round's findings."""
    if not records:
        return 0
    sets = [_finding_set(record["payload"]) for record in records]
    latest = sets[-1]
    count = 0
    for item in reversed(sets):
        if item != latest:
            break
        count += 1
    return count


def _last_finding(records: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    for record in reversed(records):
        findings = record["payload"].get("findings") or ()
        if findings:
            return findings[-1]
    return None


def _review_kind(resume_stage: Optional[str]) -> st.ArtifactKind:
    if resume_stage == st.LaneStage.WRITING_TESTS.value:
        return st.ArtifactKind.TEST_REVIEW
    return st.ArtifactKind.CODE_REVIEW


def _candidate_sha(records: Sequence[Mapping[str, Any]]) -> Optional[str]:
    for record in reversed(records):
        sha = record["payload"].get("candidate_sha")
        if sha:
            return str(sha)
    return None


def waiting_brief(store: Any, run_id: str, lane_id: str) -> str:
    """Prose for one lane's park, or `""` when the lane has never parked.

    Pure read. Every sentence is derived from a stored artifact field.
    """
    waits = store.lane_artifact_records(
        run_id, lane_id, st.ArtifactKind.USER_WAIT, 1
    )
    if not waits:
        return ""
    wait = waits[-1]
    payload = wait["payload"]
    reason = str(payload.get("wait_reason") or "")
    resume_stage = payload.get("resume_stage")
    predecessor = payload.get("predecessor_sequence")
    resume_line = RESUME_COMMAND.format(run_id=run_id)

    opening = (
        "Lane {lane} is WAITING_FOR_USER: {reason}, recorded {at} at stage {stage} "
        "over predecessor sequence {seq}.".format(
            lane=lane_id,
            reason=reason or "no reason recorded",
            at=wait.get("created_at") or "an unrecorded time",
            stage=resume_stage or "an unrecorded stage",
            seq=predecessor if predecessor is not None else "none",
        )
    )

    if reason == st.WaitReason.PAUSE.value:
        return "\n".join(
            [
                opening,
                "A pause is the operator's own interruption and carries no verdict; "
                "resuming replays the stage from the same input.",
                resume_line,
            ]
        )

    if reason != st.WaitReason.NO_PROGRESS.value:
        return "\n".join(
            [
                opening,
                "This reason is cleared by the record that caused it, not by a "
                "resume of stage {0}.".format(resume_stage or "the parked stage"),
            ]
        )

    kind = _review_kind(resume_stage)
    records = store.lane_artifact_records(run_id, lane_id, kind, REVIEW_WINDOW)
    lines = [opening]
    if records:
        lines.append(_rounds_line(records))
    else:
        lines.append("No {0} artifact is recorded on this lane.".format(kind.value))
    sha = _candidate_sha(records)
    if sha:
        lines.append("Latest candidate {0}.".format(sha))

    verdict = _classify(records)
    finding = _last_finding(records)
    if finding is not None:
        area, requirement = _finding_key(finding)
        quoted = 'The last finding is "{0}" in "{1}".'.format(requirement, area)
    else:
        quoted = "No round recorded a finding."

    if verdict == CONVERGING:
        lines.append(
            "Findings are converging: every round differs from the one before it. "
            + quoted
        )
        lines.append(
            "Resume grants another {0} rounds.".format(st.NO_PROGRESS_GRACE_ROUNDS)
        )
        lines.append(resume_line)
    elif verdict == REPEATING:
        lines.append(
            "Findings are repeating: the same finding survived {0} rounds. ".format(
                _tail_repeats(records)
            )
            + quoted
        )
        lines.append(
            "Read it against the plan before resuming; run amend if the contract "
            "is the defect."
        )
        lines.append(resume_line)
        lines.append(AMEND_COMMAND.format(run_id=run_id))
    else:
        lines.append(
            "One round is not a trend. " + quoted
        )
        lines.append(
            "Resume grants another {0} rounds.".format(st.NO_PROGRESS_GRACE_ROUNDS)
        )
        lines.append(resume_line)
    return "\n".join(lines)
