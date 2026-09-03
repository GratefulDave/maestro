#!/usr/bin/env python3
"""Stop hook: refuse a handover that carries no evidence.

Two claims cost this project whole runs, and both are cheap to make and
expensive to check by hand:

1. A `run start` / `run resume` / `run amend` line handed to the operator
   without the gate values behind it. The operator runs what is on screen.
   This hook requires that the same turn also ran `adws/tools/lane_gates.py`
   and got a table back.

2. "it's fixed" without a falsification. A fix claim is only worth the
   evidence that the assertion actually fails when the fix is removed, and
   that the copy the operator will run has the fix in it. This hook requires
   both polarities of a test run *and* a `runtime_sync.py check` result in the
   same turn's tool output.

The second rule is a mechanical proxy, and it is stated here rather than
implied: no hook can read whether a diff was reverted between two runs. What
it can see is that the turn contains a red assertion and a green run, which is
the signature a falsification leaves and which a turn that only ran the suite
once cannot produce.

Failure policy, which is the whole reason this file is standalone:

- Any error *this hook* makes -- a transcript it cannot parse, a schema it
  does not recognise, a missing file -- exits 0 and blocks nothing. A broken
  gate must never stop ordinary work.
- A missing gate table is not this hook's error. It exits 2 and blocks.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

BLOCK = 2
ALLOW = 0

#: Handing the operator any of these is handing them a command they will run.
RUN_COMMAND_PATTERNS = (
    # `\b` so that "the run started at 09:00" is prose, not a handover. The
    # trigger is the verb an operator would type, not the English word.
    r"run\s+resume\b",
    r"run\s+start\b",
    r"run\s+amend\b",
    r"maestro\.py\s+run\b",
)

NO_GATE_TABLE = (
    "no gate table in this turn; run adws/tools/lane_gates.py before handing "
    "over a run command"
)

NO_RUN_GATES = (
    "the gate table stops at the lanes; it must carry its RUN_GATES section "
    "(final review, publication preflight) before handing over a run command"
)

#: Claims that assert a defect is gone.
FIX_CLAIM_PATTERNS = (
    r"it'?s fixed",
    r"this fixes",
    r"should now",
    r"will now converge",
    r"now works",
    r"deployed",
)

NO_FALSIFICATION = (
    "a fix claim needs a falsification and a deployment check in the same turn"
)

#: What running the gate table leaves in a turn: the invocation, or its output.
GATE_TABLE_INVOCATION = re.compile(r"lane_gates\.py")
GATE_TABLE_OUTPUT = re.compile(r"^LANE_GATES ", re.MULTILINE)

#: The rows past MERGED. Lane rows alone are what let run f50638ab be handed
#: over four times: they read MERGED, MERGED, nothing stalled, while a spent
#: final review and three publication refusals sat past the last line.
RUN_GATES_OUTPUT = re.compile(r"^RUN_GATES ", re.MULTILINE)

#: A runner reporting at least one failing assertion.
RED_PATTERNS = (
    re.compile(r"\bAssertionError\b"),
    re.compile(r"^E\s+assert\b", re.MULTILINE),
    re.compile(r"\b\d+ failed\b"),
    re.compile(r"^FAILED\s", re.MULTILINE),
    # vitest's own red line. Anchored, because a bare "FAIL" anywhere in tool
    # output is not evidence that a runner reported one.
    re.compile(r"^\s*FAIL\s", re.MULTILINE),
)

#: The same runner reporting a clean pass.
GREEN_PATTERNS = (
    re.compile(r"\b\d+ passed\b"),
    re.compile(r"\bno tests? failed\b"),
    re.compile(r"^OK\b", re.MULTILINE),
)

#: A `runtime_sync.py check` that actually returned a verdict.
SYNC_INVOCATION = re.compile(r"runtime_sync\.py\s+check")
SYNC_VERDICT = re.compile(
    r"(is level|are level|LEVEL\b|compared \d+|differing|absent from|"
    r"DESTINATION_LONGER)"
)


# --------------------------------------------------------------------------
# transcript
# --------------------------------------------------------------------------


def read_transcript(path: str) -> list[Mapping[str, Any]]:
    entries: list[Mapping[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, Mapping):
                entries.append(parsed)
    return entries


def _content_blocks(entry: Mapping[str, Any]) -> Sequence[Any]:
    message = entry.get("message")
    if not isinstance(message, Mapping):
        return ()
    content = message.get("content")
    if isinstance(content, str):
        return (content,)
    if isinstance(content, list):
        return content
    return ()


def is_real_user_turn(entry: Mapping[str, Any]) -> bool:
    """True for a message the human typed, false for a tool result.

    Tool results are carried on `user` entries too, which is exactly the trap:
    reading them as turn boundaries would shrink every turn to its last tool
    call and let a handover through on the evidence of nothing.
    """
    if entry.get("type") != "user":
        return False
    for block in _content_blocks(entry):
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            return False
    return True


def current_turn(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    start = 0
    for index in range(len(entries) - 1, -1, -1):
        if is_real_user_turn(entries[index]):
            start = index + 1
            break
    return list(entries[start:])


def _text_of(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, Mapping):
        if isinstance(block.get("text"), str):
            return block["text"]
        content = block.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(_text_of(item) for item in content)
        if block.get("type") == "tool_use":
            try:
                return json.dumps(block.get("input"))
            except (TypeError, ValueError):
                return str(block.get("input"))
    return ""


def final_assistant_text(turn: Sequence[Mapping[str, Any]]) -> str:
    for entry in reversed(turn):
        if entry.get("type") != "assistant":
            continue
        parts = [
            _text_of(block)
            for block in _content_blocks(entry)
            if isinstance(block, str)
            or (isinstance(block, Mapping) and block.get("type") == "text")
        ]
        joined = "\n".join(part for part in parts if part).strip()
        if joined:
            return joined
    return ""


def turn_tool_text(turn: Sequence[Mapping[str, Any]]) -> str:
    """Every tool invocation and every tool result in this turn, as one blob."""
    parts: list[str] = []
    for entry in turn:
        for block in _content_blocks(entry):
            if not isinstance(block, Mapping):
                continue
            if block.get("type") in ("tool_use", "tool_result"):
                parts.append(_text_of(block))
        result = entry.get("toolUseResult")
        if result is not None:
            parts.append(_text_of(result) or _safe_dump(result))
    return "\n".join(part for part in parts if part)


def _safe_dump(value: Any) -> str:
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def has_gate_table(tool_text: str) -> bool:
    return bool(
        GATE_TABLE_INVOCATION.search(tool_text) and GATE_TABLE_OUTPUT.search(tool_text)
    )


def has_falsification(tool_text: str) -> bool:
    red = any(pattern.search(tool_text) for pattern in RED_PATTERNS)
    green = any(pattern.search(tool_text) for pattern in GREEN_PATTERNS)
    return red and green


def has_deployment_check(tool_text: str) -> bool:
    return bool(SYNC_INVOCATION.search(tool_text) and SYNC_VERDICT.search(tool_text))


def evaluate(final_text: str, tool_text: str) -> str | None:
    """The rejection message, or None to allow the turn to end."""
    if _matches_any(final_text, RUN_COMMAND_PATTERNS):
        if not has_gate_table(tool_text):
            return NO_GATE_TABLE
        if not RUN_GATES_OUTPUT.search(tool_text):
            return NO_RUN_GATES
    if _matches_any(final_text, FIX_CLAIM_PATTERNS) and not (
        has_falsification(tool_text) and has_deployment_check(tool_text)
    ):
        return NO_FALSIFICATION
    return None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, Mapping):
        return ALLOW
    # A hook that fires on its own block would never let the turn end.
    if payload.get("stop_hook_active"):
        return ALLOW
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return ALLOW
    turn = current_turn(read_transcript(transcript))
    if not turn:
        return ALLOW
    final_text = final_assistant_text(turn)
    if not final_text:
        return ALLOW
    reason = evaluate(final_text, turn_tool_text(turn))
    if reason is None:
        return ALLOW
    sys.stderr.write(reason + "\n")
    return BLOCK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        # This hook's own failure blocks nothing. Only a missing gate table does.
        raise SystemExit(ALLOW)
