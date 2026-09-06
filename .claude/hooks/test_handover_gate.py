"""The Stop hook blocks an unevidenced handover and nothing else.

Run from this directory: `uv run --with pytest python -m pytest test_handover_gate.py`

The cases that matter are the two directions. A hook that blocks nothing is
decoration; a hook that blocks ordinary work gets switched off within a day and
is then also decoration. So every rejection case here has a mirror that must
pass through untouched, and the fail-open cases are asserted against garbage
input rather than against a well-formed transcript.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent / "handover_gate.py"

spec = importlib.util.spec_from_file_location("handover_gate", HOOK)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


GATE_TABLE = (
    "LANE_GATES run=run-1 lane=lane-build\n"
    "FIELD    VALUE\n"
    "stage    BUILDING\n"
)


def transcript(tmp_path: Path, entries) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )
    return path


def user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def tool_call(command: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": command},
                }
            ],
        },
    }


def tool_output(text: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": text}],
        },
    }


def invoke(tmp_path: Path, entries) -> tuple[int, str]:
    payload = {
        "hook_event_name": "Stop",
        "transcript_path": str(transcript(tmp_path, entries)),
        "stop_hook_active": False,
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stderr.strip()


# --------------------------------------------------------------------------
# run commands
# --------------------------------------------------------------------------


RUN_LINES = (
    "uv run adws/maestro.py run resume run-1",
    "uv run adws/maestro.py run start --plan p",
    "uv run adws/maestro.py run amend run-1",
    "Then call maestro.py run status.",
)


@pytest.mark.parametrize("line", RUN_LINES)
def test_run_command_without_a_gate_table_is_blocked(tmp_path, line):
    code, message = invoke(
        tmp_path, [user("what now?"), assistant("Resume it:\n\n    {0}".format(line))]
    )
    assert code == gate.BLOCK
    assert message == gate.NO_GATE_TABLE


@pytest.mark.parametrize("line", RUN_LINES)
def test_run_command_with_a_gate_table_passes(tmp_path, line):
    code, message = invoke(
        tmp_path,
        [
            user("what now?"),
            tool_call("uv run adws/tools/lane_gates.py --run run-1"),
            tool_output(GATE_TABLE),
            assistant("Resume it:\n\n    {0}".format(line)),
        ],
    )
    assert (code, message) == (gate.ALLOW, "")


def test_the_invocation_alone_is_not_a_gate_table(tmp_path):
    """A command that was typed but produced no table proves nothing."""
    code, _ = invoke(
        tmp_path,
        [
            user("what now?"),
            tool_call("uv run adws/tools/lane_gates.py --run run-1"),
            tool_output("Traceback: no such file"),
            assistant("Resume with maestro.py run resume run-1"),
        ],
    )
    assert code == gate.BLOCK


def test_a_gate_table_from_an_earlier_turn_does_not_count(tmp_path):
    code, message = invoke(
        tmp_path,
        [
            user("check it"),
            tool_call("uv run adws/tools/lane_gates.py --run run-1"),
            tool_output(GATE_TABLE),
            assistant("Lane is building."),
            user("now what?"),
            assistant("Run: maestro.py run resume run-1"),
        ],
    )
    assert code == gate.BLOCK
    assert message == gate.NO_GATE_TABLE


def test_ordinary_prose_is_untouched(tmp_path):
    code, message = invoke(
        tmp_path, [user("hello"), assistant("The scheduler binds modules at import.")]
    )
    assert (code, message) == (gate.ALLOW, "")


# --------------------------------------------------------------------------
# fix claims
# --------------------------------------------------------------------------


FIX_LINES = (
    "It's fixed.",
    "This fixes the collection bug.",
    "The lane should now advance.",
    "It will now converge.",
    "Collection now works.",
    "Mirrored and deployed.",
)

RED = "E   assert 0 == 1\nAssertionError\n1 failed, 2 passed"
GREEN = "3 passed in 0.4s"
SYNC = (
    "$ python3 tools/runtime_sync.py check a b\n"
    "template and deployment are level over 231 files"
)


@pytest.mark.parametrize("line", FIX_LINES)
def test_fix_claim_without_evidence_is_blocked(tmp_path, line):
    code, message = invoke(tmp_path, [user("fix it"), assistant(line)])
    assert code == gate.BLOCK
    assert message == gate.NO_FALSIFICATION


@pytest.mark.parametrize("line", FIX_LINES)
def test_fix_claim_with_both_kinds_of_evidence_passes(tmp_path, line):
    code, message = invoke(
        tmp_path,
        [
            user("fix it"),
            tool_call("pytest tests/test_x.py"),
            tool_output(RED),
            tool_call("pytest tests/test_x.py"),
            tool_output(GREEN),
            tool_call("python3 tools/runtime_sync.py check a b"),
            tool_output(SYNC),
            assistant(line),
        ],
    )
    assert (code, message) == (gate.ALLOW, "")


def test_a_green_run_alone_is_not_a_falsification(tmp_path):
    code, message = invoke(
        tmp_path,
        [
            user("fix it"),
            tool_call("pytest tests/test_x.py"),
            tool_output(GREEN),
            tool_call("python3 tools/runtime_sync.py check a b"),
            tool_output(SYNC),
            assistant("It's fixed."),
        ],
    )
    assert code == gate.BLOCK
    assert message == gate.NO_FALSIFICATION


def test_a_falsification_without_a_sync_check_is_blocked(tmp_path):
    code, message = invoke(
        tmp_path,
        [
            user("fix it"),
            tool_call("pytest tests/test_x.py"),
            tool_output(RED),
            tool_call("pytest tests/test_x.py"),
            tool_output(GREEN),
            assistant("It's fixed."),
        ],
    )
    assert code == gate.BLOCK
    assert message == gate.NO_FALSIFICATION


# --------------------------------------------------------------------------
# fail-open on the hook's own errors, fail-closed on a missing table
# --------------------------------------------------------------------------


def _raw(payload: str) -> int:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True
    ).returncode


def test_garbage_stdin_blocks_nothing():
    assert _raw("not json at all") == gate.ALLOW


def test_empty_stdin_blocks_nothing():
    assert _raw("") == gate.ALLOW


def test_missing_transcript_blocks_nothing(tmp_path):
    assert (
        _raw(json.dumps({"transcript_path": str(tmp_path / "absent.jsonl")}))
        == gate.ALLOW
    )


def test_unparsable_transcript_blocks_nothing(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("{{{ not json\n[]\n", encoding="utf-8")
    assert _raw(json.dumps({"transcript_path": str(path)})) == gate.ALLOW


def test_recursive_stop_blocks_nothing(tmp_path):
    payload = {
        "transcript_path": str(
            transcript(tmp_path, [user("x"), assistant("maestro.py run resume r")])
        ),
        "stop_hook_active": True,
    }
    assert _raw(json.dumps(payload)) == gate.ALLOW


def test_tool_results_are_not_turn_boundaries(tmp_path):
    """A tool result is a `user` entry; reading it as a turn start empties the turn."""
    entries = [
        user("go"),
        tool_call("uv run adws/tools/lane_gates.py --run run-1"),
        tool_output(GATE_TABLE),
        assistant("maestro.py run resume run-1"),
    ]
    assert gate.is_real_user_turn(entries[0]) is True
    assert gate.is_real_user_turn(entries[2]) is False
    assert len(gate.current_turn(entries)) == 3
    code, _ = invoke(tmp_path, entries)
    assert code == gate.ALLOW
