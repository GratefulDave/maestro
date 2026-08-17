"""Typed Herdr launcher shared by omp, Claude, and offline tests.

Pane text is observability only. Lifecycle comes from Herdr state plus the
structured transcript/envelope side channel. Harness-owned subprocesses run in
dedicated process groups so inventory brackets close over a quiescent tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from .route_receipts import AdmittedRouteSet


class PollState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    GONE = "gone"


class ErrorClass(str, Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    TRANSIENT = "transient"
    PROTOCOL = "protocol"
    EXECUTION = "execution"


class HarnessCancelled(RuntimeError):
    """A harness-owned process was cancelled and its process group quiesced."""

class HarnessQuiescenceError(RuntimeError):
    """A harness-owned process group could not be proven absent."""

@dataclass(frozen=True)
class LaunchSpec:
    correlation_token: str
    worktree: Path
    prompt_path: Path
    envelope_path: Path
    route: str
    model: str
    effort: str
    profile: Optional[str]
    session_dir: Path
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LaunchHandle:
    correlation_token: str
    pane_id: str
    agent_name: str
    launched_cwd: Path
    transcript_path: Optional[Path] = None
    process_group: Optional[int] = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment",
                           MappingProxyType(dict(self.environment)))


@dataclass(frozen=True)
class PollResult:
    state: PollState
    exit_code: Optional[int] = None
    detail: str = ""


class LauncherAdapter(Protocol):
    def launch(self, spec: LaunchSpec) -> LaunchHandle: ...
    def poll(self, handle: LaunchHandle) -> PollResult: ...
    def cancel(self, handle: LaunchHandle, deadline: float) -> None: ...
    def reclaim(self, token: str) -> Tuple[LaunchHandle, ...]: ...
    def classify(self, exc: BaseException) -> ErrorClass: ...
    def provision(self, worktree: Path) -> None: ...


def build_omp_argv(binary: Path, spec: LaunchSpec) -> Tuple[str, ...]:
    if not spec.profile:
        raise ValueError("OMP_PROFILE_REQUIRED")
    argv = [
        str(binary), "--pm-profile", spec.profile,
        "--session-dir", str(spec.session_dir),
    ]
    if spec.session_dir.is_dir() and any(spec.session_dir.glob("*.jsonl")):
        argv.append("-c")
    return tuple(argv)


def build_claude_argv(binary: Path, spec: LaunchSpec) -> Tuple[str, ...]:
    return (
        str(binary), "--model", spec.model, "--effort", spec.effort,
        "--dangerously-skip-permissions", "--remote-control",
    )


class TranscriptTailer:
    """Incremental JSONL reader that never consumes an incomplete final line."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._offset = 0
        self._records = []

    def read_new(self) -> Tuple[dict, ...]:
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        boundary = chunk.rfind(b"\n")
        if boundary < 0:
            return ()
        complete = chunk[:boundary + 1]
        self._offset += len(complete)
        parsed = []
        for raw in complete.splitlines():
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                parsed.append(record)
                self._records.append(record)
        return tuple(parsed)

    def synthesized_exit(self) -> Tuple[int, str]:
        envelopes = [row for row in self._records
                     if row.get("type") == "maestro_envelope"]
        if not envelopes:
            return 1, "NO_ENVELOPE"
        if envelopes[-1].get("success") is True:
            return 0, "ENVELOPE_SUCCESS"
        return 1, "ENVELOPE_FAILURE"


def quiesce_process_group(process_group: int, deadline: float) -> None:
    """Terminate a harness-owned process group by a bounded TERM→KILL ladder."""
    # Once a reaped leader's group is absent, its numeric ID is no longer ours.
    # Probe before every terminating signal so an absent group is never reused.
    if _process_group_absent(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except OSError:
        return
    while time.monotonic() < deadline:
        if _process_group_absent(process_group):
            return
        time.sleep(0.01)
    if _process_group_absent(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except OSError:
        return
    kill_deadline = time.monotonic() + 1.0
    while time.monotonic() < kill_deadline:
        if _process_group_absent(process_group):
            return
        time.sleep(0.01)


def _process_group_absent(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def run_harness_process(
        argv: Sequence[str], *, cwd: Path,
        env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        cancel_requested: Optional[Callable[[], bool]] = None,
) -> subprocess.CompletedProcess:
    """Run one bounded, cancellable harness context in its own process group."""
    if cancel_requested is not None and cancel_requested():
        raise HarnessCancelled("HARNESS_CONTEXT_CANCELLED")
    merged = dict(os.environ)
    if env:
        merged.update(env)
    process = subprocess.Popen(
        list(argv), cwd=str(cwd), env=merged,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    # start_new_session makes the leader PID the group identity. Keep it even
    # after the leader exits: descendants can retain the captured pipes and
    # otherwise keep communicate() waiting forever.
    process_group = process.pid
    expires = None if timeout is None else time.monotonic() + timeout
    stdout = stderr = None
    try:
        while True:
            if cancel_requested is not None and cancel_requested():
                raise HarnessCancelled("HARNESS_CONTEXT_CANCELLED")
            wait_for = 0.05
            if expires is not None:
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HARNESS_CONTEXT_TIMEOUT")
                wait_for = min(wait_for, remaining)
            try:
                stdout, stderr = process.communicate(timeout=wait_for)
                break
            except subprocess.TimeoutExpired:
                if process.poll() is not None:
                    break
    except BaseException as exc:
        quiesce_process_group(process_group, time.monotonic() + 1.0)
        try:
            process.communicate(timeout=1.1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if not _process_group_absent(process_group):
            raise HarnessQuiescenceError(
                "HARNESS_CONTEXT_QUIESCENCE_UNPROVEN") from exc
        raise
    quiesce_process_group(process_group, time.monotonic() + 0.1)
    if not _process_group_absent(process_group):
        raise HarnessQuiescenceError("HARNESS_CONTEXT_QUIESCENCE_UNPROVEN")
    if stdout is None:
        stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _agent_name(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return "maestro-{}".format(digest)


def _extract(payload: dict, key: str) -> object:
    result = payload.get("result", payload)
    if isinstance(result, dict):
        if key in result:
            return result[key]
        for value in result.values():
            if isinstance(value, dict) and key in value:
                return value[key]
    return None


def _optional_int(value: object) -> Optional[int]:
    if type(value) is int:
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _available_shell(payload: Mapping[str, object]) -> bool:
    info = _extract(payload, "process_info")
    if not isinstance(info, dict):
        return False
    procs = info.get("foreground_processes")
    if not isinstance(procs, list) or len(procs) != 1:
        return False
    proc = procs[0]
    if not isinstance(proc, dict):
        return False
    shell_pid = _optional_int(info.get("shell_pid"))
    pgid = _optional_int(info.get("foreground_process_group_id"))
    proc_pid = _optional_int(proc.get("pid"))
    if None not in (shell_pid, pgid, proc_pid) and not (
            shell_pid == pgid == proc_pid):
        return False
    token = str(proc.get("name") or proc.get("argv0") or "")
    argv = proc.get("argv")
    if isinstance(argv, list) and argv:
        token = token or str(argv[0] or "")
    base = token.rsplit("/", 1)[-1].lstrip("-").lower()
    return base in ("zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh")


def _is_text_read(args: Sequence[str]) -> bool:
    """`herdr agent read` / `herdr pane read` print the snapshot as raw text.

    They have no JSON output mode (`--format` accepts only `text` and `ansi`),
    so a JSON decode failure on those commands is the normal case, not a
    protocol violation.
    """
    return len(args) >= 2 and args[0] in ("agent", "pane") and args[1] == "read"


def submit_agent_prompt(
        herdr_call: Callable[..., dict],
        pane_id: str,
        text: str,
        agent_name: Optional[str] = None,
        *,
        timeout_s: float = 30.0,
        until: Sequence[str] = ("idle",),
) -> None:
    """Submit one atomic prompt and prove the agent actually accepted it.

    `herdr agent prompt` is the documented path for coding agents: it honours
    live bracketed-paste mode and submits the text plus an encoded Enter
    atomically. `pane run` is documented for ordinary terminals, servers, and
    shells, so pointing it at a pane hosting a coding agent types the prompt as
    a shell command instead of handing it to the agent composer.

    `--wait` is what makes the submission provable. Herdr requires an observed
    lifecycle change within five seconds and returns `agent_prompt_stalled`
    otherwise. Without it a prompt delivered to a composer that is not accepting
    input yet reports success while the text simply sits on screen unsubmitted,
    which is indistinguishable from a delivered prompt until the turn times out.
    A caller timeout of five seconds or less downgrades that to a plain timeout,
    so the wait budget is always kept above it.
    """
    target = agent_name or pane_id
    wait_s = max(5.1, max(0.001, timeout_s))
    timeout_ms = str(int(wait_s * 1000))
    until_argv = []
    for status in until:
        until_argv.extend(["--until", status])
    argv = ["agent", "prompt", target, text, "--wait"]
    argv.extend(until_argv)
    argv.extend(["--timeout", timeout_ms])
    try:
        herdr_call(*argv, timeout=wait_s + 5.0)
        return
    except Exception as exc:
        if "agent_prompt_stalled" not in str(exc):
            raise
    # The composer took the text but never submitted it, so the prompt is
    # sitting on screen. Press Enter on what is already there instead of
    # prompting again -- a second `agent prompt` would append its text to the
    # unsubmitted line and send both as one garbled turn.
    herdr_call("agent", "send-keys", target, "enter", timeout=30.0)
    argv = ["agent", "wait", target]
    argv.extend(until_argv)
    argv.extend(["--timeout", timeout_ms])
    herdr_call(*argv, timeout=wait_s + 5.0)

def wait_for_interactive_agent(
        herdr_call: Callable[..., dict], name: str, timeout_s: float = 180.0,
) -> None:
    """Block until Herdr reports the coding agent idle at its composer.

    `herdr agent wait` is the documented readiness gate for coding agents. It
    replaces polling `agent get` for an undocumented `interactive_ready` field
    and scraping the visible pane for a per-agent banner string.
    """
    timeout_ms = max(1, int(max(0.001, timeout_s) * 1000))
    try:
        herdr_call(
            "agent", "wait", name, "--until", "idle",
            "--timeout", str(timeout_ms),
            timeout=timeout_s + 5.0)
    except RuntimeError as exc:
        raise RuntimeError(
            "AGENT_INTERACTIVE_READY_TIMEOUT:{}".format(name)) from exc

def _wait_for_available_shell(
        herdr_call: Callable[..., dict], pane_id: str, timeout_s: float = 30.0,
        settle_polls: int = 5,
) -> None:
    """Wait until the pane is a settled interactive shell.

    A single ready snapshot is not enough: a freshly split pane can look like a
    lone zsh in the gap before login hooks (direnv, keychain lookups) spawn
    their own foreground processes. Starting an agent in that gap makes Herdr
    report `agent_pane_busy`. Require several consecutive ready snapshots so the
    pane has demonstrably stopped changing before we start the agent.
    """
    deadline = time.monotonic() + timeout_s
    ready = 0
    while True:
        try:
            payload = herdr_call("pane", "process-info", "--pane", pane_id)
        except RuntimeError:
            payload = {}
        ready = ready + 1 if _available_shell(payload) else 0
        if ready >= settle_polls:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("LAUNCH_REFUSED:SHELL_NOT_READY")
        time.sleep(0.1)




class HerdrLauncher:
    def __init__(self, *, herdr_path: Path, omp_path: Path, claude_path: Path,
                 admitted_routes: AdmittedRouteSet,
                 provision_argv: Sequence[str] = ()) -> None:
        if not isinstance(admitted_routes, AdmittedRouteSet):
            raise TypeError("VERIFIED_ADMITTED_ROUTES_REQUIRED")
        self.herdr_path = Path(herdr_path)
        self.omp_path = Path(omp_path)
        self.claude_path = Path(claude_path)
        self.admitted_routes = admitted_routes
        self.provision_argv = tuple(provision_argv)
        self._handles_lock = threading.RLock()
        self._handles: Dict[str, LaunchHandle] = {}
        self._tailers: Dict[str, TranscriptTailer] = {}
        self._proven_absent: Dict[str, LaunchHandle] = {}

    def _herdr(self, *args: str, env: Optional[Mapping[str, str]] = None,
               timeout: float = 30.0) -> dict:
        merged = dict(os.environ)
        if env:
            merged.update(env)
        try:
            result = subprocess.run(
                [str(self.herdr_path), *args], capture_output=True, text=True,
                env=merged, timeout=timeout, check=False,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("LAUNCH_REFUSED:{}".format(exc)) from exc
        if result.returncode != 0:
            raise RuntimeError("LAUNCH_REFUSED:{}".format((result.stderr or result.stdout).strip()[-400:]))
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            if _is_text_read(args):
                return {"result": {"text": result.stdout or ""}}
            raise RuntimeError("PROTOCOL_INVALID_JSON") from exc
        if not isinstance(payload, dict):
            if _is_text_read(args):
                return {"result": {"text": result.stdout or ""}}
            raise RuntimeError("PROTOCOL_INVALID_RESPONSE")
        return payload

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        if not self.admitted_routes.admits(spec.route):
            raise RuntimeError("ROUTE_NOT_ADMITTED:{}".format(spec.route))
        worktree = spec.worktree.resolve()
        environment = MappingProxyType(dict(spec.environment))
        split = self._herdr("pane", "split", "--current", "--direction", "right",
                            "--cwd", str(worktree), "--no-focus", env=environment)
        pane = _extract(split, "pane")
        if not isinstance(pane, dict) or not pane.get("pane_id"):
            raise RuntimeError("LAUNCH_REFUSED:NO_PANE")
        pane_id = str(pane["pane_id"])
        name = _agent_name(spec.correlation_token)
        route_argv = (build_omp_argv(self.omp_path, spec) if spec.route == "omp"
                      else build_claude_argv(self.claude_path, spec)
                      if spec.route == "claude" else None)
        if route_argv is None:
            raise ValueError("UNSUPPORTED_ROUTE:{}".format(spec.route))
        current = self._herdr("pane", "get", pane_id, env=environment)
        bound = _extract(current, "pane")
        actual = (Path(str(bound.get("cwd"))).resolve()
                  if isinstance(bound, dict) and bound.get("cwd") else None)
        if actual != worktree:
            try:
                self._herdr("pane", "close", pane_id, env=environment)
            except BaseException:
                pass
            raise RuntimeError("BINDING_MISMATCH:{}!={}".format(actual, worktree))
        try:
            _wait_for_available_shell(
                lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
                pane_id)
            started = self._herdr(
                "agent", "start", name, "--kind", spec.route,
                "--pane", pane_id, "--timeout", "180000",
                "--", *route_argv[1:],
                env=environment, timeout=185.0)
        except BaseException:
            try:
                self._herdr("pane", "close", pane_id, env=environment)
            except BaseException:
                pass
            raise
        current = self._herdr("pane", "get", pane_id, env=environment)
        bound = _extract(current, "pane")
        actual = (Path(str(bound.get("cwd"))).resolve()
                  if isinstance(bound, dict) and bound.get("cwd") else None)
        if actual != worktree:
            self.cancel(LaunchHandle(spec.correlation_token, pane_id, name,
                                     actual or Path("/"),
                                     environment=environment),
                        time.monotonic() + 1.0)
            raise RuntimeError("BINDING_MISMATCH:{}!={}".format(actual, worktree))
        agent = _extract(started, "agent")
        transcript = None
        if isinstance(agent, dict) and agent.get("transcript_path"):
            transcript = Path(str(agent["transcript_path"]))
        handle = LaunchHandle(spec.correlation_token, pane_id, name, worktree,
                              transcript_path=transcript, environment=environment)
        with self._handles_lock:
            self._handles[spec.correlation_token] = handle
            self._proven_absent.pop(spec.correlation_token, None)
            if transcript:
                self._tailers[spec.correlation_token] = TranscriptTailer(transcript)
        wait_for_interactive_agent(
            lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
            name)
        bootstrap = "@{0}".format(spec.prompt_path.resolve())
        # Settle for either working or idle: the harness turn runs for as long
        # as the task takes, so waiting for idle here would hold the launch open
        # for the whole run, while a short task can be back at idle before the
        # working state is ever sampled.
        submit_agent_prompt(
            lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
            pane_id, bootstrap, name,
            timeout_s=60.0, until=("working", "idle"))
        return handle

    def poll(self, handle: LaunchHandle) -> PollResult:
        payload = self._herdr("agent", "get", handle.agent_name,
                              env=handle.environment)
        agent = _extract(payload, "agent")
        if not isinstance(agent, dict):
            return PollResult(PollState.GONE, detail="AGENT_GONE")
        status = str(agent.get("status") or agent.get("agent_status") or "unknown")
        with self._handles_lock:
            tailer = self._tailers.get(handle.correlation_token)
        progressed = False
        if tailer:
            tailer.read_new()
            progressed = bool(tailer._records)
        if progressed:
            code, detail = tailer.synthesized_exit()
            return PollResult(PollState.EXITED, code, detail)
        if status in ("starting", "unknown"):
            return PollResult(PollState.STARTING)
        return PollResult(PollState.RUNNING)

    def cancel(self, handle: LaunchHandle, deadline: float) -> None:
        token = handle.correlation_token
        with self._handles_lock:
            if self._proven_absent.get(token) is handle:
                return
        if handle.process_group is not None:
            try:
                quiesce_process_group(handle.process_group, deadline)
            except BaseException as exc:
                raise HarnessQuiescenceError(
                    "HERDR_QUIESCENCE_UNPROVEN:{}".format(token)) from exc
            if not _process_group_absent(handle.process_group):
                raise HarnessQuiescenceError(
                    "HERDR_QUIESCENCE_UNPROVEN:{}".format(token))
        try:
            closed = _extract(
                self._herdr("pane", "close", handle.pane_id,
                            env=handle.environment),
                "closed")
            if closed is not True:
                raise RuntimeError("PANE_CLOSE_UNCONFIRMED:{}".format(
                    handle.pane_id))
            state = self.poll(handle)
            if state.state is not PollState.GONE:
                raise RuntimeError("PANE_STILL_LIVE:{}".format(handle.pane_id))
        except BaseException as exc:
            raise HarnessQuiescenceError(
                "HERDR_QUIESCENCE_UNPROVEN:{}".format(token)) from exc
        with self._handles_lock:
            if self._handles.get(token) is handle:
                self._handles.pop(token)
                self._tailers.pop(token, None)
                self._proven_absent[token] = handle

    def reclaim(self, token: str) -> Tuple[LaunchHandle, ...]:
        with self._handles_lock:
            handle = self._handles.get(token)
        return (handle,) if handle is not None else ()

    def classify(self, exc: BaseException) -> ErrorClass:
        return classify_error(exc)

    def provision(self, worktree: Path) -> None:
        if not self.provision_argv:
            return
        result = run_harness_process(self.provision_argv, cwd=worktree, timeout=600)
        if result.returncode != 0:
            raise RuntimeError("PROVISION_FAILED:{}".format(result.stderr[-400:]))


class FakeLauncher:
    def __init__(self) -> None:
        self._handles: Dict[str, LaunchHandle] = {}
        self._states: Dict[str, PollResult] = {}

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        handle = LaunchHandle(spec.correlation_token, "fake:" + spec.correlation_token,
                              _agent_name(spec.correlation_token), spec.worktree.resolve())
        self._handles[spec.correlation_token] = handle
        self._states[spec.correlation_token] = PollResult(PollState.RUNNING)
        return handle

    def complete(self, token: str, exit_code: int = 0,
                 detail: str = "ENVELOPE_SUCCESS") -> None:
        self._states[token] = PollResult(PollState.EXITED, exit_code, detail)

    def poll(self, handle: LaunchHandle) -> PollResult:
        return self._states.get(handle.correlation_token,
                                PollResult(PollState.GONE, detail="AGENT_GONE"))

    def cancel(self, handle: LaunchHandle, deadline: float) -> None:
        self._states[handle.correlation_token] = PollResult(PollState.GONE,
                                                            detail="CANCELLED")

    def reclaim(self, token: str) -> Tuple[LaunchHandle, ...]:
        handle = self._handles.get(token)
        return (handle,) if handle is not None else ()

    def classify(self, exc: BaseException) -> ErrorClass:
        return classify_error(exc)

    def provision(self, worktree: Path) -> None:
        return None


def classify_error(exc: BaseException) -> ErrorClass:
    if isinstance(exc, FileNotFoundError):
        return ErrorClass.CONFIGURATION
    if isinstance(exc, PermissionError):
        return ErrorClass.AUTHENTICATION
    if isinstance(exc, TimeoutError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return ErrorClass.PROTOCOL
    return ErrorClass.EXECUTION
