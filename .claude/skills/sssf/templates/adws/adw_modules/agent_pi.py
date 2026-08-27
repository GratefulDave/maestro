"""omp coding agent interface — v1's only coding agent.

Runs `omp -p --mode json` and tails its JSONL stdout line by line, forwarding
each event to a callback WHILE the agent works (the streaming crack, solved by
construction).

Three things here are shaped by what omp 17.3.1 actually accepts, measured on
2026-08-13 rather than read from documentation:

* There is no `--list-models` flag. The catalog is the `omp models --json`
  subcommand, which reports provider, id, and context window together.
* There is no `--session-id`. A session is identified by the directory it
  lives in, and `-c` continues the newest session there. Since each agent
  already gets its own session directory, "same directory plus -c" is what
  "same session id = same context window" means on this route.
* The model omp reports back is not always the model it was asked for: a
  request for `openai-codex/gpt-5.6-luna` runs `openai-codex/gpt-5.6-terra`.
  So the reported binding is checked against the requested one and a
  substitution raises rather than being recorded as the model that ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult
from .utils import now_iso, operator_env


class ModelBindingError(RuntimeError):
    """omp ran a different model than the roster asked for.

    Recording the requested model when another one answered would put a
    number in the trace that no run produced, so this is fatal rather than a
    warning.
    """


def omp_path() -> str:
    """The omp binary, read at call time so a run can point at another one."""
    return os.environ.get("OMP_PATH") or os.environ.get("PI_PATH") or "omp"

RESULT_SNIPPET_CHARS = 20_000   # tool output rides along whole; clip only guards pathological cases
ARG_VALUE_CHARS = 20_000        # args too — the UI scrolls, it must not be handed cut-off data
LABEL_CHARS = 80                # "bash: <command>" shown as the event name

# The arg that identifies a call at a glance, in the order tools tend to use.
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")


@lru_cache(maxsize=1)
def catalog() -> tuple:
    """omp's merged model catalog, as `(provider, model_id, context_window)`.

    `omp models --json` reports the same merged view the picker shows —
    built-in providers plus anything registered locally — and carries the
    context window with each row, so this is the only place model facts are
    read from.
    """
    try:
        result = subprocess.run(
            [omp_path(), "models", "--json"], capture_output=True, text=True,
            timeout=30, env=operator_env(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    try:
        listed = json.loads(result.stdout).get("models", [])
    except (json.JSONDecodeError, AttributeError):
        return ()
    rows = []
    for model in listed:
        provider, model_id = model.get("provider"), model.get("id")
        if not provider or not model_id:
            continue
        rows.append((provider, model_id, int(model.get("contextWindow") or 0)))
    return tuple(rows)


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve a model pattern to an explicit ``(provider, model_id)`` pair.

    omp's catalog merges built-in models with whatever is registered locally.
    Using that same merged view lets SSSF target direct providers such as
    ``openai-codex/gpt-5.6-terra`` without re-registering built-in models.
    """
    listed = [(provider, model_id) for provider, model_id, _ in catalog()]
    # omp routing suffixes are not catalog entries.
    #
    # A profile picks its own model and may append a routing alias:
    # `~/.omp/profiles/deepseek/agent/config.yml` sets
    # `defaultModel: deepseek/deepseek-v4-flash:auto`, and `:auto` asks omp to
    # pick a route at call time. The catalog enumerates the model
    # (`deepseek/deepseek-v4-flash`) and some concrete aliases (`:free`), but
    # never `:auto`, so resolving the profile's own string raised
    # `model pattern ... not found in `omp models`` -- which B13 reports as
    # HandoffTooLarge, because a model whose window cannot be read cannot be
    # shown to fit one. run-6357251adc7d41dc9b2a72645f778c9c spent all seven
    # attempts there at zero turns, having launched no agent at all.
    #
    # The suffix selects a route, not a context window, so the window to
    # measure against is the base model's. Strip it and resolve the base.
    if ":" in pattern:
        base, _, suffix = pattern.rpartition(":")
        if base and (base, suffix) not in (
            (provider, model_id.split("/", 1)[-1]) for provider, model_id in listed
        ) and not any(model_id.endswith(":" + suffix) for _, model_id in listed):
            pattern = base
    if "/" in pattern:
        provider, model_id = pattern.split("/", 1)
        if (provider, model_id) in listed:
            return provider, model_id
    matches = [(provider, model_id) for provider, model_id in listed
               if pattern == model_id or pattern in model_id]
    exact = [match for match in matches
             if match[1] == pattern or match[1].endswith("/" + pattern)]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"model pattern {pattern!r} not found in `omp models` — "
                         "authenticate/register it or fix the config")
    raise ValueError(f"model pattern {pattern!r} is ambiguous: {matches}")


def _context_tokens(usage: dict) -> int:
    """Tokens occupying the window after a turn.

    Mirrors pi's own `calculateContextTokens` (coding-agent
    `core/compaction/compaction.ts`), which is what pi compacts against and
    shows in its footer: prefer the provider's `totalTokens`, else sum the
    parts. Cache reads count — cached prompt is still prompt.
    """
    total = usage.get("totalTokens") or 0
    if total:
        return int(total)
    return int(sum(usage.get(part) or 0
                   for part in ("input", "output", "cacheRead", "cacheWrite")))


def context_window(provider: str, model_id: str) -> int:
    """The model's context ceiling from omp's merged model catalog."""
    for listed_provider, listed_model, window in catalog():
        if listed_provider == provider and listed_model == model_id:
            return window
    return 0


def _text_of(container: dict) -> str:
    """Join the text blocks of anything pi shapes as {content: [...]} — a
    message or a tool result."""
    return "".join(part.get("text", "") for part in container.get("content", []) or []
                   if isinstance(part, dict) and part.get("type") == "text")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _label(tool: str, args: dict) -> str:
    """One-line human name for a tool call: `bash: ls -la src`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool}: {_clip(value, LABEL_CHARS)}" if value else tool


class ToolCallTracker:
    """Folds pi's tool stream into ONE normalized record per completed call.

    pi announces a call as a `toolCall` content block, then emits
    tool_execution_start / _update / _end for it. Only the end carries the
    result, so that is where a record is emitted — one trace event per real
    tool call, the moment it returns, instead of three shapeless ones.

    The record carries the call's real span (`started_at`/`ended_at`), which the
    tracer writes to columns so the UI can lay tool calls on a time axis without
    parsing every payload.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        """Returns the record for a finished tool call, else None."""
        etype = event.get("type", "")
        if etype == "message_end":
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._announce(block.get("id"), block.get("name"),
                                   block.get("arguments"))
            return None
        if etype == "tool_execution_start":
            self._announce(event.get("toolCallId"), event.get("toolName"),
                           event.get("args"))
            return None
        if etype != "tool_execution_end":
            return None

        call_id = str(event.get("toolCallId") or "")
        opened = self._open.pop(call_id, {})
        tool = str(event.get("toolName") or opened.get("tool") or "tool")
        args = event.get("args") or opened.get("args") or {}
        record = {
            "tool": tool,
            "tool_call_id": call_id,
            "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": not event.get("isError", False),
            "label": _label(tool, args),
        }
        result_text = _text_of(event.get("result") or {})
        if result_text:
            record["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
        record["ended_at"] = now_iso()
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        if opened.get("started_at"):
            record["started_at"] = opened["started_at"]
        return record

    def _announce(self, call_id, tool, args) -> None:
        """First sighting starts the clock; a later sighting only fills gaps."""
        if not call_id:
            return
        known = self._open.get(str(call_id), {})
        self._open[str(call_id)] = {
            "tool": tool or known.get("tool", ""),
            "args": args or known.get("args", {}),
            "started_at": known.get("started_at") or now_iso(),   # wall clock, for the row
            "clock": known.get("clock") or time.monotonic(),      # monotonic, for duration
        }


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive pi turn.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable — a hung coding agent is otherwise a pid you have
    to hunt for in `ps` while the run sits there.
    """
    sessions = Path(request.session_dir)
    cmd = [
        omp_path(), "-p", "--mode", "json",
        "--session-dir", str(sessions),
        "--system-prompt", request.system_prompt,
    ]
    if request.pm_profile:
        cmd += ["--profile", request.pm_profile]
        requested = ""
        result = PiResult(session_id=request.session_id, context_window=0)
    else:
        provider, model_id = resolve_model(request.model)
        requested = "{}/{}".format(provider, model_id)
        cmd[4:4] = ["--provider", provider, "--model", model_id,
                    "--thinking", request.thinking]
        result = PiResult(session_id=request.session_id,
                          context_window=context_window(provider, model_id))
    # omp has no --session-id, so rejoining a context window is "the same
    # session directory, continued". Each agent already owns its directory
    # (§12.2 step 1 item 4 keys it by agent, node, and attempt), so -c
    # continues that agent's own session and nobody else's. The first run in a
    # directory has nothing to continue and must not ask.
    if sessions.is_dir() and any(sessions.glob("*.jsonl")):
        cmd.append("-c")
    if request.tools:
        cmd += ["--tools", ",".join(request.tools)]
    for extension in request.extensions:
        cmd += ["-e", extension]
    cmd.append(request.prompt)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    ran = set()
    # stdin is DEVNULL, deliberately. The prompt travels in argv, so the child
    # never needs stdin — but inheriting the parent's means pi sees a non-TTY
    # and can sit forever waiting for piped input that will never arrive or
    # EOF. That failure is silent and total: no request goes out, no bytes come
    # back, and the ADW blocks on a read loop with nothing to read. Observed as
    # a run that sat idle at 0% CPU with an empty raw_output.jsonl.
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    with raw_path.open("a") as raw:
        assert process.stdout is not None
        for line in process.stdout:
            raw.write(line)
            raw.flush()                      # events land on disk as they happen
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                message = event.get("message", {})
                # The binding omp reports, turn by turn. A real run's first
                # message_end is the user echo and names no model at all, so
                # only a turn that states one is evidence of what ran.
                if message.get("model"):
                    ran.add("{}/{}".format(message.get("provider") or "",
                                           message.get("model")))
                if message.get("role") == "assistant":
                    text = _text_of(message)
                    if text:
                        result.text = text   # last assistant message wins
                    usage = message.get("usage", {}) or {}
                    turn = _context_tokens(usage)
                    result.tokens += turn
                    result.usage.add_turn(usage, turn)
                    # Occupancy is read off the last VALID assistant turn, the
                    # way pi does it — an aborted or errored turn reports usage
                    # you can't trust, so it must not overwrite a good reading.
                    if turn and message.get("stopReason") not in ("aborted", "error"):
                        result.context_tokens = turn
                    result.cost += (usage.get("cost", {}) or {}).get("total", 0.0) or 0.0
            if on_event:
                on_event(event)

    stderr = process.stderr.read() if process.stderr else ""
    result.returncode = process.wait()
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0 and not result.text:
        raise RuntimeError(f"omp exited {result.returncode}: {stderr.strip()[-800:]}")
    # §9.5's binding verification, on the model rather than the binary. omp
    # answers some requests with a different model than it was handed, and a
    # run that records the request rather than the answer reports a model that
    # never produced a token.
    if request.pm_profile:
        result.model_ran = sorted(ran)[-1] if ran else ""
        return result
    substituted = sorted(binding for binding in ran if binding != requested)
    if substituted:
        raise ModelBindingError(
            "omp was asked for {} and ran {} — the roster's model is not what "
            "answered".format(requested, ", ".join(substituted)))
    result.model_ran = requested if ran else ""
    return result
