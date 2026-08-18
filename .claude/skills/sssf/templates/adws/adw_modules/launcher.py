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
from typing import (Callable, Dict, List, Mapping, Optional, Protocol,
                    Sequence, Tuple)

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


class LaunchRefusal(Enum):
    """One typed launcher refusal, with the two structural facts about it.

    The refusal codes have always travelled in the exception's message
    (`LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:...`), and §7.5 forbids any
    caller branching on that prose — matching the prefix to pick a retry class
    is the lexical shortcut an AST test convicts. So each code is a member
    here and carries what callers actually need to know, as fields:

    * `pane_created` — whether a pane existed at the moment the refusal was
      raised. §8.3's quiesce step proves an attempt's owned execution absent,
      and §16.3 item 45 records what happens without this fact: a refusal
      raised *before* the split hits a mandatory `finally: quiesce`, which
      raises `PROCESS_GROUP_UNTRACKED` over a process that was never started,
      and Python replaces the launch's own exception with it. The naive repair
      — treating every failed launch as proven absent — lies for the refusals
      raised *after* the split (`SHELL_NOT_READY`, `NO_PANE`), where a pane
      really does exist and its group is exactly what quiescence is for.
      Reported rather than inferred, so the proof is skipped only where
      absence is established by construction.
    * `deterministic` — whether another attempt could plausibly survive what
      this one did not. §7.5 closes the retry classes at three and makes the
      closure load-bearing, so this is *not* a fourth class: it sizes the
      budget of a member inside LAUNCHER_TRANSIENT, exactly as
      `LauncherFailure.CREDENTIAL`'s zero already does ("the budget is a
      property of the member, not of the class"). A call site that omits an
      environment omits it identically on every attempt, and spending two
      more launches on it ends in `LAUNCHER_BUDGET_EXHAUSTED` — a reason that
      says a budget ran out when nothing was ever retryable (§16.3 item 46).

    Both default conservatively at every reader: an untyped launch failure is
    quiesced as before and retried as before.
    """

    #: Raised while building the split's own arguments, so no pane exists and
    #: none can. Deterministic: the environment is computed by the call site.
    SCRATCH_REDIRECT_MISSING = ("SCRATCH_REDIRECT_MISSING", False, True)
    #: Refused before any herdr call, against the verified admitted-route set.
    ROUTE_NOT_ADMITTED = ("ROUTE_NOT_ADMITTED", False, True)
    #: The split returned without a pane id. §16.3 item 45 names this among
    #: the post-split refusals: herdr may hold a pane it did not report.
    NO_PANE = ("NO_PANE", True, False)
    #: The pane exists and never settled into an interactive shell.
    SHELL_NOT_READY = ("SHELL_NOT_READY", True, False)

    def __init__(self, code: str, pane_created: bool, deterministic: bool) -> None:
        self.code = code
        self.pane_created = pane_created
        self.deterministic = deterministic


class LaunchRefused(RuntimeError):
    """A launcher refusal that names its own code as a typed member.

    Subclasses `RuntimeError` and keeps the exact `LAUNCH_REFUSED:<code>[:...]`
    message the operator-facing ledger already carries, so nothing that reads
    the string changes. What is new is `refusal`, which is what callers branch
    on — the same separation `HerdrCallError` already draws between `.code`
    and the message Herdr may reword at any release.
    """

    def __init__(self, refusal: LaunchRefusal, detail: str = "") -> None:
        super().__init__("LAUNCH_REFUSED:{}{}".format(
            refusal.code, ":" + detail if detail else ""))
        self.refusal = refusal
        self.detail = detail

    @property
    def pane_created(self) -> bool:
        return self.refusal.pane_created

    @property
    def deterministic(self) -> bool:
        return self.refusal.deterministic


class HarnessCancelled(RuntimeError):
    """A harness-owned process was cancelled and its process group quiesced."""

class HarnessQuiescenceError(RuntimeError):
    """A harness-owned process group could not be proven absent."""


class HerdrCallError(RuntimeError):
    """A refused `herdr` call, carrying Herdr's own structured error code.

    §1.2 forbids keying a lifecycle decision on prose. Herdr answers a refused
    call with `{"error": {"code": ..., "message": ...}}`: the code is a typed
    field, the message is free text Herdr may reword at any release. Callers
    that must branch on *why* a call was refused read `.code`. The string form
    is preserved unchanged so operator-facing detail is unaffected.
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


def herdr_error_code(text: str) -> str:
    """Herdr's `error.code` from a refused call's output, or `""`.

    Anything that does not parse, or parses without an `error.code`, yields the
    empty string rather than a guess: an unrecognised refusal must not be
    mistaken for a recognised one, which is what a substring match on the
    message does.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


#: Herdr's refusal when it holds no record of the requested agent. A finished
#: agent whose session has exited is reported this way, not as an agent with an
#: empty record.
AGENT_NOT_FOUND = "agent_not_found"

#: §8.3's cache-redirection variables, named here rather than only beside
#: `worktree.scratch_env`, because this module is where they cross the herdr
#: boundary. A variable absent from this tuple never reaches the agent's shell
#: however carefully it was computed.
SCRATCH_ENV_KEYS: Tuple[str, ...] = (
    "XDG_CACHE_HOME",
    "TMPDIR",
    "PYTHONPYCACHEPREFIX",
    "PYTEST_ADDOPTS",
    "COVERAGE_FILE",
    "RUFF_CACHE_DIR",
    "npm_config_cache",
)


def pane_env_flags(environment: Mapping[str, str]) -> Tuple[str, ...]:
    """`--env KEY=VALUE` flags carrying §8.3's redirection into the pane shell.

    The environment this process passes to the `herdr` CLI does not reach the
    pane. `herdr` is a client: it hands the split over a socket to the herdr
    server, and the server forks the pane's shell from *its own* environment,
    so `env=` on the CLI subprocess stops at the client. Measured 2026-08-17
    against herdr 0.8.0: a variable exported into the CLI subprocess is absent
    from the pane's shell, while the same variable passed as `--env` to `pane
    split` is present in it. `herdr agent start` has no environment option of
    its own and needs none — it starts the agent at the pane's own shell
    prompt, so the agent inherits whatever the split established.

    The incident this closes: an agent node whose pane never received
    `PYTHONPYCACHEPREFIX` or `PYTEST_ADDOPTS` ran its own tests, wrote 226
    `.pyc` files and a `.pytest_cache` into its worktree, and was convicted
    under §8.3's permission check for the harness's own omission. The
    harness's pre-gate, started as an ordinary subprocess with the same
    mapping, honoured the redirect in the same attempt — which is exactly the
    asymmetry that identifies the boundary.

    Only the redirection variables are forwarded. The rest of the launch
    environment is the harness's own, and the pane already has the operator's.

    Missing variables are refused rather than skipped. §8.3's preference order
    is redirect, then suppress, then the write convicts, and a redirect that
    silently fails to arrive convicts an agent for a harness defect. A refusal
    here happens before any untrusted code runs and names the variable.
    """
    missing = [key for key in SCRATCH_ENV_KEYS if not environment.get(key)]
    if missing:
        raise LaunchRefused(LaunchRefusal.SCRATCH_REDIRECT_MISSING,
                            ",".join(missing))
    flags: List[str] = []
    for key in SCRATCH_ENV_KEYS:
        flags.extend(("--env", "{}={}".format(key, environment[key])))
    return tuple(flags)


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
    envelope_path: Optional[Path] = None
    #: Honestly unreachable rather than mistakenly unwired, and the difference
    #: is why this field stays. §8.3 states it plainly: for an agent node the
    #: process is spawned by the herdr server, and herdr 0.8.0's recorded
    #: surface (§9.1) exposes no pid and no process group, so there is no group
    #: Maestro owns and no kill it can aim — agent-node settle is measurement,
    #: not termination. §16.3 item 17 makes adopting one conditional on an
    #: executed §9.8 receipt that does not exist yet: either herdr exposing the
    #: agent pid with a guaranteed dedicated group, or `herdr agent start`
    #: exec'ing an adapter-supplied group-leader wrapper verbatim, with the
    #: receipt showing the group excludes the pane shell and every sibling
    #: attempt. On that receipt §8.3's code-node quiesce extends to agent nodes
    #: unchanged, and this is the field it writes into. Deleting it as "unused"
    #: would delete the seam the receipt is meant to fill; the dead-seam sweep
    #: therefore carries it as deliberate rather than deferred.
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

    def terminal_envelope(self) -> Optional[Tuple[int, str]]:
        """The turn's own verdict, or `None` if the turn has not declared.

        Distinguished from `synthesized_exit` because that method answers
        "what exit code should this attempt get" and folds *absence* into
        failure — it returns `NO_ENVELOPE` as exit 1 whether the agent failed
        or has simply not finished. A caller deciding whether the turn is over
        at all must not read that 1 as an answer, which is exactly the
        conflation that scored a completed successful turn as a failure.
        """
        if not any(row.get("type") == "maestro_envelope"
                   for row in self._records):
            return None
        return self.synthesized_exit()


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


def _extract(payload: Mapping[str, object], key: str) -> object:
    """One field out of a herdr reply, from the envelope or one level in.

    `Mapping`, not `dict`: every read below is a lookup or an iteration and
    nothing here mutates, while two callers hold a `Mapping[str, object]` —
    `_available_shell` and `_agent_transcript_path` — and were passing it into
    a `dict` annotation. Widening the parameter to what the function actually
    requires is the honest direction; narrowing the callers would have been
    annotating around a constraint that does not exist.
    """
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


def _agent_transcript_path(agent: object) -> Optional[Path]:
    """Where herdr says this agent's transcript lives.

    `agent start` and `agent get` report it as `agent_session`, a tagged value
    whose `kind` says how to read `value` -- `path` for the routes that write a
    JSONL transcript. Reading only a flat `transcript_path` key, which herdr
    does not send, left every launch without a session path and failed the node
    with SESSION_PATH_MISSING before its first turn.
    """
    if not isinstance(agent, dict):
        return None
    direct = agent.get("transcript_path")
    if direct:
        return Path(str(direct))
    session = agent.get("agent_session")
    if isinstance(session, dict) and session.get("kind") == "path":
        value = session.get("value")
        if value:
            return Path(str(value))
    return None


class PromptNotSubmitted(RuntimeError):
    """The composer holds the prompt text and will not submit it.

    Raised only after every recovery attempt has been spent, so it means the
    pane is genuinely wedged rather than merely slow.
    """


#: How many times to press Enter on an unsubmitted composer before giving up.
#: More than one because a single fallback is what failed in the recorded
#: incident: the Enter went in while the composer was still not accepting
#: input, the one verification wait then blocked for the whole remaining
#: budget, and the 600s window expired around a prompt that was never sent.
SUBMIT_ATTEMPTS = 4


def pane_revision(herdr_call: Callable[..., dict], pane_id: str) -> Optional[int]:
    """The pane's monotonic revision counter, or None when it cannot be read.

    A typed integer the terminal maintains, not pane text: §1.2 permits reading
    it, and it is the one signal that separates an agent that consumed a prompt
    from one whose composer is still holding it. `agent_status` cannot — a pane
    that never accepted the prompt and a pane whose short turn already finished
    both report `idle`.
    """
    try:
        payload = herdr_call("pane", "get", pane_id, timeout=15.0)
    except Exception:
        return None
    pane = (payload or {}).get("result", {}).get("pane")
    if not isinstance(pane, dict):
        pane = payload if isinstance(payload, dict) else {}
    revision = pane.get("revision")
    return revision if isinstance(revision, int) else None


def submit_agent_prompt(
        herdr_call: Callable[..., dict],
        pane_id: str,
        text: str,
        agent_name: Optional[str] = None,
        *,
        timeout_s: float = 30.0,
        until: Sequence[str] = ("idle",),
        attempts: int = SUBMIT_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
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

    **Why this is a loop and not a fallback.** Against omp the stall happens
    roughly half the time: the composer takes the `@<path>` text and never
    submits it. A single Enter afterwards is not enough — if it lands while the
    composer still is not accepting input it is swallowed, and the one
    verification wait that follows then consumes the whole budget waiting on a
    prompt that was never sent. So each Enter is followed by a *short* verify,
    and a failed verify presses again. The budget buys several observations
    instead of one.

    The recovery presses Enter on what is already on screen and never re-issues
    `agent prompt`: a second prompt would append its text to the unsubmitted
    line and send both as one garbled turn.
    """
    target = agent_name or pane_id
    total_s = max(5.1, max(0.001, timeout_s))
    until_argv: List[str] = []
    for status in until:
        until_argv.extend(["--until", status])
    # What the pane had consumed before the prompt was offered. Everything
    # below compares against this rather than against a status word.
    baseline = pane_revision(herdr_call, pane_id)

    def consumed() -> bool:
        """Whether the pane has taken anything since the prompt was offered.

        `idle` is worthless as proof here and was the whole defect: a composer
        holding an unsubmitted `@<path>` reports `idle`, so waiting for it
        succeeded instantly against exactly the failure it existed to catch,
        and the Enter recovery below never ran once. The revision counter
        cannot be satisfied that way -- an unsubmitted composer does not
        advance it.

        Unreadable on either side means unproven, and unproven is not
        submitted: the caller's next move is to press Enter again, which is
        harmless against a prompt that did go through.
        """
        if baseline is None:
            return False
        current = pane_revision(herdr_call, pane_id)
        return current is not None and current > baseline

    def wait_for(budget_s: float) -> bool:
        argv = ["agent", "wait", target, *until_argv,
                "--timeout", str(int(budget_s * 1000))]
        try:
            herdr_call(*argv, timeout=budget_s + 5.0)
        except Exception:
            return False
        return consumed()

    argv = ["agent", "prompt", target, text, "--wait", *until_argv,
            "--timeout", str(int(total_s * 1000))]
    try:
        herdr_call(*argv, timeout=total_s + 5.0)
        if consumed():
            return
    except Exception as exc:
        if "agent_prompt_stalled" not in str(exc):
            raise

    rounds = max(1, attempts)
    # Kept above herdr's own five-second lifecycle-observation floor, or the
    # verify degrades into a plain timeout that proves nothing.
    per_round = max(5.1, total_s / rounds)
    for round_no in range(rounds):
        try:
            herdr_call("agent", "send-keys", target, "enter", timeout=30.0)
        except Exception:
            # A send-keys that fails on one round is not fatal: the pane may be
            # mid-repaint. Fall through to the verify, which is the thing that
            # actually decides.
            pass
        if wait_for(per_round):
            return
        if round_no + 1 < rounds:
            sleep(0.5)
    raise PromptNotSubmitted(
        "AGENT_PROMPT_UNSUBMITTED:{0} after {1} submit attempts".format(
            target, rounds))

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


#: How long `launch` waits for herdr to report where the agent's transcript is.
#: Bounded at the prompt submission's own 60s rather than the 180s readiness
#: gate: by the time this runs the agent has already been waited to idle at its
#: composer, so a session file that has not appeared within a minute is absent
#: rather than late, and the caller's SESSION_PATH_MISSING is then correct.
TRANSCRIPT_PATH_TIMEOUT_S = 60.0


def wait_for_agent_transcript(
        herdr_call: Callable[..., dict], name: str, timeout_s: float,
        poll_interval_s: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
) -> Optional[Path]:
    """Poll `agent get` until herdr reports this agent's transcript path.

    `agent start` returns once herdr holds the process, which for a route that
    writes a JSONL transcript is before the coder has created the file. herdr
    then omits `agent_session` entirely, and reading the start payload alone
    made SESSION_PATH_MISSING a race: of three attempts on one node on
    2026-08-17, the one that happened to win it ran 61 turns and the two that
    lost it died at turn zero.

    Whether the path has arrived is read from the typed `agent_session.kind`
    field, never from the pane (§1.2). `None` at the deadline rather than a
    raise: the absence is the caller's to classify, and a route that writes no
    transcript at all is not an error here.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            payload = herdr_call("agent", "get", name)
        except RuntimeError:
            # A transport hiccup, or herdr not yet holding the record, is a
            # missing observation and not an answer. Keep polling to the
            # deadline rather than converting it into a decision.
            payload = {}
        transcript = _agent_transcript_path(_extract(payload, "agent"))
        if transcript is not None:
            return transcript
        if time.monotonic() >= deadline:
            return None
        sleep(poll_interval_s)


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
            raise LaunchRefused(LaunchRefusal.SHELL_NOT_READY)
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
            refusal = (result.stderr or result.stdout).strip()
            raise HerdrCallError(
                "LAUNCH_REFUSED:{}".format(refusal[-400:]),
                herdr_error_code(refusal))
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
            raise LaunchRefused(LaunchRefusal.ROUTE_NOT_ADMITTED, spec.route)
        worktree = spec.worktree.resolve()
        environment = MappingProxyType(dict(spec.environment))
        # The pane shell is forked by the herdr server, not by the CLI process
        # below, so `env=` alone leaves the bracket's redirection outside the
        # pane entirely (§8.3). `--env` is the only surface that crosses.
        split = self._herdr("pane", "split", "--current", "--direction", "right",
                            "--cwd", str(worktree), "--no-focus",
                            *pane_env_flags(environment), env=environment)
        pane = _extract(split, "pane")
        if not isinstance(pane, dict) or not pane.get("pane_id"):
            raise LaunchRefused(LaunchRefusal.NO_PANE)
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
        transcript = _agent_transcript_path(agent)
        handle = LaunchHandle(spec.correlation_token, pane_id, name, worktree,
                              transcript_path=transcript,
                              envelope_path=spec.envelope_path,
                              environment=environment)
        with self._handles_lock:
            self._handles[spec.correlation_token] = handle
            self._proven_absent.pop(spec.correlation_token, None)
            if transcript:
                self._tailers[spec.correlation_token] = TranscriptTailer(transcript)
        wait_for_interactive_agent(
            lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
            name)
        if transcript is None:
            # The start payload is a snapshot taken before the coder opened its
            # session, not a statement that this route has no transcript. Poll
            # for the typed path now that the agent is idle at its composer, so
            # the caller's SESSION_PATH_MISSING means absent rather than early.
            transcript = wait_for_agent_transcript(
                lambda *args, **kwargs: self._herdr(*args, env=environment,
                                                    **kwargs),
                name, TRANSCRIPT_PATH_TIMEOUT_S)
            if transcript is not None:
                # The handle is already registered, and `_handles` must keep
                # naming the same object the caller holds -- same frozen-field
                # assignment `__post_init__` uses, rather than a second handle
                # the registry and the caller could disagree about.
                object.__setattr__(handle, "transcript_path", transcript)
                with self._handles_lock:
                    self._tailers[spec.correlation_token] = TranscriptTailer(
                        transcript)
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

    def agent_status(self, handle: LaunchHandle) -> Optional[str]:
        """The route's raw per-pane status, uncollapsed — B14's seam.

        `poll` cannot serve this. For a build node it must read `idle` as "the
        turn ended", collapsing the very distinction B14 needs: *not yet
        started*, *working*, and *stopped without declaring* all arrive as
        `idle` there. So the raw string is exposed separately, and
        `FinalizationWindow` does the arming (idle only counts once the agent
        has been seen working).

        `None` on any read failure or a vanished agent, never a guess — an
        unreadable status is a missing observation, and the window treats it as
        such rather than as a stall.
        """
        try:
            payload = self._herdr("agent", "get", handle.agent_name,
                                  env=handle.environment)
        except RuntimeError:
            return None
        agent = _extract(payload, "agent")
        if not isinstance(agent, dict):
            return None
        raw = agent.get("agent_status") or agent.get("status")
        return str(raw) if raw else None

    def _agent_absent(self, handle: LaunchHandle) -> bool:
        """Whether Herdr still holds a record of this agent.

        `cancel` needs this and *not* `poll`. They answer different questions:
        `poll` asks what the attempt's outcome is, `cancel` asks whether the
        agent is gone. They were the same call until the artifact was given
        precedence in `poll`, at which point sharing them would have made every
        *successful* attempt -- the ones that leave an envelope on disk --
        report EXITED to `cancel`, which reads anything but GONE as
        `PANE_STILL_LIVE` and raises `HERDR_QUIESCENCE_UNPROVEN`. Quiescence is
        a fact about the process, never about the work it produced.
        """
        try:
            payload = self._herdr("agent", "get", handle.agent_name,
                                  env=handle.environment)
        except HerdrCallError as exc:
            # An agent whose pane is closed is not reported as an agent with no
            # record: herdr exits nonzero with `agent_not_found`. Reading that
            # as an error rather than as absence makes `cancel` treat a
            # successful close as unproven quiescence, which blocks every agent
            # node. Keyed on the typed code, never the message (§1.2).
            if exc.code != AGENT_NOT_FOUND:
                raise
            return True
        return not isinstance(_extract(payload, "agent"), dict)

    def _declared_result(self, handle: LaunchHandle) -> Optional[PollResult]:
        """The turn's own declaration, or `None` if it has not declared.

        Reads only what the agent WROTE -- the envelope file it was told to
        write, then the transcript's terminal record for routes that declare
        there. Never the pane, never a status, never prose (§1.2).

        The envelope path is per-attempt
        (`.../scratch/<run>-<node>-a<N>/agent-envelope.json`), so a previous
        attempt's envelope cannot be mistaken for this one's.
        """
        envelope = handle.envelope_path
        if envelope is not None and envelope.is_file():
            try:
                payload = json.loads(envelope.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                return PollResult(PollState.EXITED, 1, "ENVELOPE_UNPARSED")
            success = isinstance(payload, dict) and payload.get("success") is True
            return PollResult(PollState.EXITED, 0 if success else 1,
                              "ENVELOPE_SUCCESS" if success else "ENVELOPE_FAILURE")
        with self._handles_lock:
            tailer = self._tailers.get(handle.correlation_token)
        # `terminal_envelope` returns None rather than a failing exit when the
        # turn has not declared, so an unfinished turn cannot be mistaken for a
        # failed one.
        if tailer is not None:
            tailer.read_new()
            declared = tailer.terminal_envelope()
            if declared is not None:
                exit_code, detail = declared
                return PollResult(PollState.EXITED, exit_code, detail)
        return None

    def poll(self, handle: LaunchHandle) -> PollResult:
        # ── the artifact is read BEFORE the agent is observed ───────────────
        #
        # It used to be read after, and the two GONE returns below sat in front
        # of it. That lost every attempt whose agent finished fast: `herdr agent
        # get` answers `agent_not_found` as soon as the finished agent's session
        # exits, so a complete `"success": true` envelope already on disk was
        # never opened and the attempt was scored GONE, retried as
        # ENVIRONMENTAL, and eventually failed the node with
        # ENVIRONMENTAL_BUDGET_EXHAUSTED. It is a race the *fast* agent loses:
        # the sooner it declares and exits, the likelier the next poll sees the
        # empty record first. Observed on run
        # run-14b7b75944094c52ac9c0add41ae46a2, whose three attempts each wrote
        # a valid success envelope and were each thrown away.
        #
        # GONE is now reachable only when the agent is gone AND nothing was
        # declared, which is what GONE was always supposed to mean.
        declared = self._declared_result(handle)
        if declared is not None:
            return declared
        try:
            payload = self._herdr("agent", "get", handle.agent_name,
                                  env=handle.environment)
        except HerdrCallError as exc:
            if exc.code != AGENT_NOT_FOUND:
                raise
            return PollResult(PollState.GONE, detail="AGENT_GONE")
        agent = _extract(payload, "agent")
        if not isinstance(agent, dict):
            return PollResult(PollState.GONE, detail="AGENT_GONE")
        status = str(agent.get("status") or agent.get("agent_status") or "unknown")
        with self._handles_lock:
            tailer = self._tailers.get(handle.correlation_token)
        turns = 0
        if tailer:
            tailer.read_new()
            turns = len(tailer._records)

        # Reaching here means `_declared_result` found nothing: the turn has
        # not declared, so the pane's status is all there is to go on.
        #
        # The attempt ends when the agent writes its terminal envelope. The
        # envelope is a file the agent is told to write, not a record in the
        # route's transcript: an interactive route writes its own event schema
        # and has no reason to emit one of ours, so reading the transcript for
        # a terminal record ended every attempt on the agent's first message
        # and cancelled the work mid-flight.
        # ── precedence: what the agent WROTE beats what the pane REPORTS ────
        #
        # `agent_status` is a lagging observation of a pane; a written envelope
        # is the turn's own declaration. Three real failures came from letting
        # an observation of the pane win. A completed turn behind a stale
        # `working` status held the poll in RUNNING until a wall clock expired
        # and charged the attempt ENVIRONMENTAL; a successful turn observed at
        # `idle` fell through to the no-envelope branch and scored a passing
        # attempt as exit 1; and a successful turn whose agent had already
        # exited was scored GONE because the *absence* of the pane was read
        # before the envelope it had already written.
        #
        # This is the same rule `FinalizationWindow.poll` follows when it
        # checks for the report before consulting any signal, and it does not
        # contradict B14's arming rule. B14 says an `idle` that has never been
        # seen working is *not yet started* and must not be read as dead. This
        # says a status of any kind is stale once the turn has declared. One is
        # about absence of output before liveness, the other about presence of
        # output after it; both resolve the same way — the artifact wins.
        if status in ("starting", "unknown"):
            return PollResult(PollState.STARTING)
        # Idle after at least one turn and still no envelope: the agent
        # finished its turn without writing one. That is a failed attempt, not
        # a running one -- waiting would hold the node until its timeout with
        # nothing left to observe.
        if status == "idle" and turns:
            return PollResult(PollState.EXITED, 1, "NO_ENVELOPE")
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
            # herdr confirms a close as `{"result": {"type": "ok"}}`; there is
            # no `closed` flag. Demanding one turned every successful close
            # into PANE_CLOSE_UNCONFIRMED, which is raised inside the block
            # that proves quiescence, so the proof could never succeed.
            response = self._herdr("pane", "close", handle.pane_id,
                                   env=handle.environment)
            result = response.get("result")
            closed = _extract(response, "closed")
            confirmed = (closed is True
                         or (isinstance(result, dict)
                             and result.get("type") == "ok"))
            if not confirmed:
                raise RuntimeError("PANE_CLOSE_UNCONFIRMED:{}".format(
                    handle.pane_id))
            # `_agent_absent`, not `poll`: a successful attempt leaves an
            # envelope, and `poll` now reports that declaration in preference
            # to any observation of the pane. Asking `poll` here would read a
            # success as EXITED, conclude the pane was still live, and refuse
            # quiescence for every node that worked.
            if not self._agent_absent(handle):
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
        self._statuses: Dict[str, Optional[str]] = {}

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        handle = LaunchHandle(spec.correlation_token, "fake:" + spec.correlation_token,
                              _agent_name(spec.correlation_token), spec.worktree.resolve())
        self._handles[spec.correlation_token] = handle
        self._states[spec.correlation_token] = PollResult(PollState.RUNNING)
        return handle

    def complete(self, token: str, exit_code: int = 0,
                 detail: str = "ENVELOPE_SUCCESS") -> None:
        self._states[token] = PollResult(PollState.EXITED, exit_code, detail)

    def set_agent_status(self, token: str, status: Optional[str]) -> None:
        self._statuses[token] = status

    def agent_status(self, handle: LaunchHandle) -> Optional[str]:
        return self._statuses.get(handle.correlation_token)

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
