"""Typed direct-Claude process route.

This preserves the established ``PiRequest``/``PiResult`` seam while invoking
Claude directly. OMP is never used as a proxy for this route.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Callable, Optional, Tuple

from .agent_pi import ModelBindingError
from .data_types import PiRequest, PiResult, UsageBreakdown
from .launcher import (HarnessQuiescenceError, _process_group_absent,
                       prepare_route_prompt_text, quiesce_process_group)
from .utils import operator_env


STDERR_TAIL_BYTES = 8 * 1024
STDERR_READ_BYTES = 8 * 1024
PROCESS_GRACE_SECONDS = 1.0

# Step 8's signed Claude route receipt proves this one stable alias-to-canonical
# relationship.  This is deliberately a finite table: accepting every
# ``claude-opus-*`` string would turn a model binding check into a prefix check.
CLAUDE_CANONICAL_MODELS = {
    "opus": "claude-opus-5",
}


def _reported_models(event: dict) -> tuple:
    """Return the model identities Claude included in one stream-json event."""
    models = []

    def add(model: object) -> None:
        if isinstance(model, str) and model:
            models.append(model)

    add(event.get("model"))
    message = event.get("message")
    if isinstance(message, dict):
        add(message.get("model"))
    model_usage = event.get("modelUsage")
    if isinstance(model_usage, dict):
        for model in model_usage:
            add(model)
    return tuple(models)


def _drain_stderr(stream, tail: bytearray) -> None:
    """Continuously retain only the final bounded stderr bytes."""
    try:
        while True:
            chunk = os.read(stream.fileno(), STDERR_READ_BYTES)
            if not chunk:
                return
            tail.extend(chunk)
            if len(tail) > STDERR_TAIL_BYTES:
                del tail[:-STDERR_TAIL_BYTES]
    except (OSError, ValueError):
        # Cleanup closes the pipe only after its process group is quiesced.
        return


def _text(message: dict) -> str:
    """Preserve Claude's exact adjacency between neighboring text blocks."""
    return "".join(str(part.get("text", "")) for part in message.get("content") or []
                   if isinstance(part, dict) and part.get("type") == "text")


def claude_path() -> str:
    return os.environ.get("CLAUDE_PATH", str(Path.home() / ".local/bin/claude"))


def _claude_session_id(logical_id: str) -> str:
    try:
        return str(uuid.UUID(logical_id))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "maestro:" + logical_id))


def _scope_paths(request: PiRequest) -> Tuple[Path, Path, Path]:
    """Return isolated state paths for one exact logical session/model pair."""
    identity = "{}\0{}".format(request.session_id, request.model).encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()
    scope = Path(request.session_dir) / "claude" / key
    return scope, scope / "marker.json", scope / Path(request.raw_output_path).name


def _resume_id(marker: Path, request: PiRequest) -> str:
    """Use a persisted identity only when it verifies this exact request."""
    try:
        recorded = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(recorded, dict):
        return ""
    session_id = recorded.get("claude_session_id")
    if (recorded.get("logical_session_id") != request.session_id
            or recorded.get("model") != request.model
            or not isinstance(session_id, str) or not session_id):
        return ""
    return session_id


def _write_marker(marker: Path, request: PiRequest, claude_session_id: str) -> None:
    """Atomically persist only the stream identity verified after a good run."""
    payload = {
        "logical_session_id": request.session_id,
        "model": request.model,
        "claude_session_id": claude_session_id,
    }
    temporary = marker.with_name("{}.{}.tmp".format(marker.name, uuid.uuid4().hex))
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True))
        os.replace(temporary, marker)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _tool_base(tool: str) -> str:
    """Return the built-in Claude tool enabled by one exact allowlist entry."""
    base = tool.partition("(")[0]
    if not base:
        raise ValueError("Claude direct route received an empty tool capability")
    if base.startswith("mcp__"):
        raise ValueError(
            "Claude direct route does not support MCP tool capabilities; refusing "
            "to spawn with an unmapped capability")
    return base


def _builtin_tools(tools: list[str]) -> list[str]:
    """Deduplicate enabled built-ins while retaining allowlist entries exactly."""
    builtins = []
    for tool in tools:
        base = _tool_base(tool)
        if base not in builtins:
            builtins.append(base)
    return builtins


def validate_capabilities(tools: Optional[list[str]], extensions: list[str]) -> None:
    """Reject capabilities that this direct CLI route cannot enforce."""
    if extensions:
        raise ValueError(
            "Claude direct route does not support PiRequest.extensions; refusing "
            "to spawn with unmapped capabilities")
    if tools is not None:
        _builtin_tools(tools)


def _argv(request: PiRequest, resume: str) -> list:
    """Build the direct-Claude command without exposing the user prompt in argv."""
    command = [
        claude_path(), "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", request.model, "--effort", request.thinking,
        "--system-prompt", request.system_prompt,
    ]
    if request.dangerously_skip_permissions:
        command.append("--dangerously-skip-permissions")
    if request.tools is not None:
        builtins = _builtin_tools(request.tools)
        if builtins:
            command += ["--tools", *builtins, "--allowedTools", *request.tools]
        else:
            # Claude documents an empty --tools value as the no-tools mode.
            command += ["--tools", ""]
        command += ["--disallowedTools", "mcp__*"]
    if resume:
        command += ["--resume", resume]
    else:
        command += ["--session-id", _claude_session_id(request.session_id)]
    return command


def _capture(state: dict, key: str, error: BaseException) -> None:
    if state.get(key) is None:
        state[key] = (error, error.__traceback__)


def _observe_event(event: dict, result: PiResult, usage: UsageBreakdown,
                   state: dict) -> None:
    """Fold one parsed event into the typed result while checking terminality."""
    if state["result_count"]:
        state["event_after_result"] = True
    state["models"].update(_reported_models(event))

    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id:
        state["sessions"].add(session_id)

    event_type = event.get("type")
    if event_type == "assistant":
        message = event.get("message")
        if isinstance(message, dict):
            text = _text(message)
            if text:
                result.text = text       # last assistant message remains authoritative
    elif event_type == "result":
        state["result_count"] += 1
        if isinstance(event.get("result"), str) and event["result"]:
            result.text = event["result"]
        result.cost = float(event.get("total_cost_usd") or 0.0)
        raw_usage = event.get("usage") or {}
        if isinstance(raw_usage, dict):
            usage.input_tokens = int(raw_usage.get("input_tokens") or 0)
            usage.output_tokens = int(raw_usage.get("output_tokens") or 0)
            usage.cache_read_tokens = int(raw_usage.get("cache_read_input_tokens") or 0)
            usage.cache_write_tokens = int(raw_usage.get("cache_creation_input_tokens") or 0)
        usage.total_tokens = (usage.input_tokens + usage.output_tokens
                              + usage.cache_read_tokens + usage.cache_write_tokens)
        usage.total_cost = result.cost
        if event.get("is_error"):
            state["result_error"] = True


def _drain_stdout(stream, raw_path: Path, result: PiResult, usage: UsageBreakdown,
                  state: dict, on_event: Optional[Callable[[dict], None]]) -> None:
    """Drain stdout for the process lifetime, even when a callback has failed."""
    try:
        with raw_path.open("ab") as raw:
            for line in iter(stream.readline, b""):
                raw.write(line)
                raw.flush()
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                _observe_event(event, result, usage, state)
                if on_event and state.get("event_callback") is None:
                    try:
                        on_event(event)
                    except BaseException as error:
                        _capture(state, "event_callback", error)
                        state["abort"].set()
    except BaseException as error:
        _capture(state, "reader_error", error)
        state["abort"].set()


def _write_prompt(stream, prompt: str, state: dict) -> None:
    """Feed the prompt through stdin so it never appears in process listings."""
    try:
        stream.write(prompt.encode("utf-8"))
        stream.close()
    except BrokenPipeError as error:
        _capture(state, "stdin_error", RuntimeError(
            "claude closed stdin before receiving the prompt"))
        state["abort"].set()
    except BaseException as error:
        _capture(state, "stdin_error", error)
        state["abort"].set()


def _raise_captured(captured) -> None:
    error, traceback = captured
    raise error.with_traceback(traceback)


def _primary_error(request: PiRequest, state: dict, code: int,
                   resume: str) -> Optional[
                       Tuple[BaseException, Optional[TracebackType]]]:
    """Return the one failure that must win over an on_exit callback failure."""
    for key in ("event_callback", "reader_error", "stdin_error"):
        if state.get(key) is not None:
            return state[key]
    if code != 0:
        stderr = bytes(state["stderr_tail"]).decode("utf-8", "replace").strip()[-800:]
        return RuntimeError("claude exited {}: {}".format(code, stderr)), None
    if state["result_count"] != 1 or state["event_after_result"]:
        return RuntimeError("claude did not emit exactly one terminal result event"), None
    if state["result_error"]:
        return RuntimeError("claude reported an error result"), None
    if not state["models"]:
        return ModelBindingError(
            "Claude did not report a model identity for requested {}".format(request.model)), None
    admitted = {request.model}
    canonical = CLAUDE_CANONICAL_MODELS.get(request.model)
    if canonical:
        admitted.add(canonical)
    substituted = sorted(model for model in state["models"] if model not in admitted)
    if substituted:
        return ModelBindingError(
            "Claude was asked for {} and ran {} — the roster's model is not what "
            "answered".format(request.model, ", ".join(substituted))), None
    if len(state["sessions"]) != 1:
        return RuntimeError(
            "Claude did not report exactly one session identity for logical session {}"
            .format(request.session_id)), None
    reported_session = next(iter(state["sessions"]))
    if resume and reported_session != resume:
        return RuntimeError(
            "Claude resumed {} but reported session {}".format(resume, reported_session)), None
    return None


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one isolated non-interactive Claude turn and verify its terminal result."""
    validate_capabilities(request.tools, request.extensions)
    scope, marker, raw_path = _scope_paths(request)
    scope.mkdir(parents=True, exist_ok=True)
    resume = _resume_id(marker, request)
    process = subprocess.Popen(
        _argv(request, resume), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, cwd=request.cwd, env=operator_env(),
        start_new_session=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    result = PiResult(session_id=request.session_id)
    usage = UsageBreakdown()
    state = {
        "abort": threading.Event(),
        "stderr_tail": bytearray(),
        "models": set(),
        "sessions": set(),
        "result_count": 0,
        "result_error": False,
        "event_after_result": False,
        "event_callback": None,
        "reader_error": None,
        "stdin_error": None,
    }
    stdout_thread = threading.Thread(
        target=_drain_stdout,
        args=(process.stdout, raw_path, result, usage, state, on_event),
        name="claude-stdout-drain",
    )
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(process.stderr, state["stderr_tail"]),
        name="claude-stderr-drain",
    )
    stdin_thread = threading.Thread(
        target=_write_prompt,
        args=(process.stdin,
              prepare_route_prompt_text("claude", request.prompt), state),
        name="claude-stdin-write",
    )
    stdout_thread.start()
    stderr_thread.start()
    primary = None
    quiescence_error = None
    stdin_started = False
    try:
        if on_spawn:
            on_spawn(process.pid)
        stdin_thread.start()
        stdin_started = True
        while process.poll() is None:
            if state["abort"].is_set():
                quiesce_process_group(process.pid, time.monotonic()
                                      + PROCESS_GRACE_SECONDS)
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
    except BaseException as error:
        primary = (error, error.__traceback__)
        state["abort"].set()
    finally:
        # A leader can exit while a descendant still owns either pipe.  Quiesce
        # the session before joining readers so no inherited descriptor can keep
        # this call blocked forever.
        quiesce_process_group(process.pid, time.monotonic()
                              + PROCESS_GRACE_SECONDS)
        if not _process_group_absent(process.pid):
            quiescence_error = HarnessQuiescenceError(
                "CLAUDE_PROCESS_GROUP_QUIESCENCE_UNPROVEN")
        if process.poll() is None:
            try:
                process.wait(timeout=PROCESS_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                if primary is None:
                    primary = (RuntimeError("claude leader did not exit after cleanup"), None)
        if quiescence_error is not None:
            # A failed proof cannot be allowed to pin a reader on an inherited
            # descriptor forever.  This path is already terminally failed.
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            process.stdin.close()
        except OSError:
            pass
        if stdin_started:
            stdin_thread.join()
        stdout_thread.join()
        stderr_thread.join()
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass

    if primary is None and quiescence_error is not None:
        primary = (quiescence_error, None)
    if primary is None:
        primary = _primary_error(request, state, process.returncode, resume)
    if primary is None:
        reported_session = next(iter(state["sessions"]))
        canonical = CLAUDE_CANONICAL_MODELS.get(request.model)
        actual = canonical if canonical in state["models"] else sorted(state["models"])[0]
        result.session_id = reported_session
        result.model_ran = "claude/{}".format(actual)
        result.usage = usage
        result.tokens = usage.total_tokens
        result.context_tokens = usage.total_tokens
        try:
            _write_marker(marker, request, reported_session)
        except BaseException as error:
            primary = (error, error.__traceback__)

    exit_error = None
    if on_exit:
        try:
            on_exit(process.pid)
        except BaseException as error:
            exit_error = (error, error.__traceback__)
    if primary is not None:
        _raise_captured(primary)
    if exit_error is not None:
        _raise_captured(exit_error)
    return result
