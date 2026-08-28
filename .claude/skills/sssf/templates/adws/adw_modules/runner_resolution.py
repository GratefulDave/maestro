"""Which binary runs a gate — declared, probed, recorded (§1.2, §7.5).

`Gate.runner` is `Literal["pytest", "vitest"]` (`plan_model.RUNNERS`), and
until this module existed that literal became `argv[0]` directly, in three
places, and every one of them was executed with an inherited `PATH`. The
binary that adjudicated a node was therefore whatever the shell that started
the scheduler happened to expose.

Measured cost of that inheritance, in the repository this was written against:

    $ which pytest                     -> /opt/homebrew/bin/pytest (8.4.0)
    $ pytest --collect-only -q …       -> ModuleNotFoundError: No module named 'structlog'
    $ .venv/bin/pytest --collect-only  -> 10 tests collected

Same worktree, same argv, opposite outcomes. The damage is not that the gate
goes red; it is *how* it goes red. `worktree._run_gate` parses counts by regex
over the runner's output, a conftest `ImportError` prints no summary line, so
`counts == {}`, `GateCounts.parse` returns `None`, `adjudicate_gate` stamps
`RetryClass.ENVIRONMENTAL`, and the node re-runs an identically broken
interpreter until `ENVIRONMENTAL_BUDGET_EXHAUSTED`. A permanently wrong
interpreter presents as a transient fault and the operator is told a budget
ran out when nothing was ever retryable — the same shape
`LauncherFailure.DETERMINISTIC_REFUSAL` was added to kill on the launcher side.

Three rules hold this module together.

**One producer, one carrier.** `resolve()` is the only function that produces a
`ResolvedRunner`, and `ResolvedRunner` is the only thing `worktree._run_gate`
and `plan_validate.SubprocessCollector` will accept. That is enforced by
signature rather than by convention: after this change a new call site that
wants to run a gate has nothing to build an invocation *from*. The 2026-08-18
handoff size check sat on one launch path while another route skipped it
(§19 M6); the answer is not to call a checker from three places, it is to
delete the type that let three places build a command.

**Declaration binds, discovery only proposes.** `maestro.config.yaml` may
carry a `runners:` block keyed by the runner literal, exactly as
`executables:` already declares `herdr`, `omp`, and `claude` — the runner was
the one external binary Maestro executed without such a declaration. A
declared runner is probed and, if the probe fails, refuses; it never falls
back to a guess, because a declaration that silently falls back is not a
declaration. Only when the block is absent does discovery run, and its output
must still survive the probe and is written to the run record, so which
interpreter executed is a recorded input rather than an ambient fact.

**Capability is an exit code, never stderr text.** §1.2 forbids keying a
lifecycle decision on prose. The gate's own exit code cannot discriminate
either — measured, a broken interpreter on an existing test file and a good
interpreter on a not-yet-written test file *both* exit 4 (`USAGE_ERROR`), and
both collect zero cases. So capability is asked separately, by a probe that is
decoupled from the plan's selectors: walk the whole tree, select nothing. A
capable runner imports every conftest and every test module, matches nothing,
and exits `NO_TESTS_COLLECTED`; an incapable one dies during import.

    $ .venv/bin/pytest -p no:cacheprovider -o addopts= --collect-only -q \
          -k maestro_runner_probe_no_match
    no tests collected (2789 deselected) in 10.10s          exit 5
    $ /opt/homebrew/bin/pytest  (same argv)
    E ModuleNotFoundError: No module named 'structlog'      exit 4
    $ /opt/homebrew/bin/python3 -m pytest  (same argv)
    No module named pytest                                  exit 1

`5` is public stable API — `pytest.ExitCode.NO_TESTS_COLLECTED` — not folklore.
`-o addopts=` is mandatory in the probe as well as in collection: a repository
setting `-W error` there turns a capable probe into `MAX_WARNINGS_ERROR` (6).
`-p no:cacheprovider` keeps the probe from writing `.pytest_cache` into the
tree it is judging. The probe costs about ten seconds, once per `(runner,
cwd)`, because it imports every test module; a cheaper probe that skipped that
import would miss a conftest that only fails on a plugin a test module pulls
in.

**Vitest uses its native list mode.** Measured against Vitest 3.2.4:
`vitest list --run --testNamePattern maestro_runner_probe_no_match` loads the
repository test graph, selects no cases, prints nothing, and exits 0. A broken
configuration or module import exits non-zero, so the same capability rule
applies without parsing output.

**The resolution is not part of the plan digest.** `plan_model.to_plan_nodes`
still projects `gate_command` as the abstract `(runner,) + argv`, and
`_assert_projection_is_total` still asserts exactly that. Baking a resolved
interpreter into the projection would make the binary part of the approved
bytes, change every plan digest, and stop a plan being portable between
machines. Resolution is an execution-time fact, deliberately outside identity.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Tuple)

#: The collection flags each runner needs, without its binary. This is the old
#: `plan_validate.COLLECT_ARGV` with `argv[0]` removed — the binary is what
#: this module exists to decide, so a table that carries one is the defect.
#:
#: pytest reads `addopts` from the repository's own ini file and prepends it,
#: so a repository carrying `-v` there cancels the `-q` here: verbosity nets to
#: the default and `--collect-only` prints its tree form (`<Function x>`)
#: instead of flat `path::case` identifiers, which reads as zero collected
#: cases. `-o addopts=` clears the inherited options.
COLLECT_ARGS: Dict[str, Tuple[str, ...]] = {
    "pytest": ("--collect-only", "-q", "-o", "addopts="),
    "vitest": ("list", "--run"),
}

#: The flags each runner needs to EXECUTE cases once, without its binary, and
#: the exact counterpart of `COLLECT_ARGS` above.
#:
#: `plan_contract_ingress._parse_verifier_command` strips the `run`
#: sub-command out of an authored `npx vitest run <paths>`, because the runner
#: is `vitest` and `run` is that runner's mode rather than part of the gate's
#: argv. Collection re-supplied its own mode through `COLLECT_ARGS`;
#: execution re-supplied nothing, so every vitest gate executed as bare
#: `vitest <paths>` -- which is vitest's WATCH mode. It ran the cases, printed
#: the report, and then sat forever waiting for a file to change. The gate
#: never returned, the pre-gate never finished, no agent pane was ever
#: allocated, and on `run-9d03105407f440079f3730f1fe4c67b3` every attempt of
#: `lane-wp6-build` died at the harness timeout without once launching.
#:
#: pytest has no such mode and its entry is empty on purpose: the table is
#: keyed by runner so the asymmetry is visible rather than implied.
EXECUTE_ARGS: Dict[str, Tuple[str, ...]] = {
    "pytest": (),
    "vitest": ("run",),
}

#: The capability probe: walk the whole tree, select nothing. Decoupled from
#: the plan's selectors on purpose — see the module docstring for why the
#: gate's own exit code cannot answer this question.
PROBE_ARGS: Dict[str, Tuple[str, ...]] = {
    "pytest": ("-p", "no:cacheprovider", "-o", "addopts=", "--collect-only",
               "-q", "-k", "maestro_runner_probe_no_match"),
    "vitest": ("list", "--run", "--testNamePattern",
               "maestro_runner_probe_no_match"),
}

#: The exit code a capable runner returns from its probe. Measured, never
#: guessed. Both values are stable runner behavior rather than parsed prose.
CAPABLE_EXIT: Dict[str, int] = {
    "pytest": 5,  # pytest.ExitCode.NO_TESTS_COLLECTED
    "vitest": 0,  # vitest list with a test-name filter that matches nothing
}

#: How a runner is asked for its own version. Recorded as provenance in the run
#: record and never branched on, so §1.2 is untouched by it.
VERSION_ARGS: Tuple[str, ...] = ("--version",)

PROBE_TIMEOUT_S = 180.0
VERSION_TIMEOUT_S = 30.0


class Reason(str, Enum):
    """Why a runner is unusable — a closed enum on the payload.

    Three sibling refusal codes were considered and rejected: the
    discriminating fact travels as a typed member, never as a message prefix,
    for the same reason `LauncherFailure` carries its budget as a member
    rather than as a `LAUNCH_REFUSED:` string.
    """

    #: Nothing resolved to an executable file at all, or the runner has no
    #: measured probe, so capability cannot be established.
    UNRESOLVED = "UNRESOLVED"
    #: An executable resolved and started, and could not import the
    #: repository's collection prerequisites.
    INCAPABLE = "INCAPABLE"
    #: Two candidates at the same discovery rank. Choosing between them would
    #: be the unrecorded policy this design exists to remove.
    AMBIGUOUS = "AMBIGUOUS"


#: The one run-start refusal outcome. Not a fourth `RetryClass` — §7.5 closes
#: that set at three and there is nothing to retry. Not a new `BlockReason` —
#: a block reason describes a node that ran and stopped, and under this design
#: no node ever starts. This is a run precondition, the same family as
#: `INTEGRATION_BRANCH_CHECKED_OUT`: refuse at run start, launch nothing,
#: write no attempt row, reach no classifier.
RUNNER_UNUSABLE = "RUNNER_UNUSABLE"


class RunnerUnusable(RuntimeError):
    """A gate runner that cannot be used, with the reason as a typed field."""

    def __init__(self, runner: str, reason: Reason, cwd: str, *,
                 candidates: Sequence[str] = (),
                 resolved: Optional[str] = None,
                 probe_exit: Optional[int] = None,
                 detail: str = "") -> None:
        self.runner = runner
        self.reason = reason
        self.cwd = cwd
        self.candidates: Tuple[str, ...] = tuple(candidates)
        self.resolved = resolved
        self.probe_exit = probe_exit
        self.detail = detail or self._detail()
        super().__init__(self.detail)

    def _detail(self) -> str:
        if self.reason is Reason.AMBIGUOUS:
            return ("two {0} candidates share a discovery rank: {1}; declare "
                    "one under `runners:` in maestro.config.yaml".format(
                        self.runner, ", ".join(self.candidates)))
        if self.reason is Reason.INCAPABLE:
            return ("{0} resolved to {1}, which started and could not collect "
                    "in {2} (probe exit {3}); it cannot import this "
                    "repository's test prerequisites".format(
                        self.runner, self.resolved, self.cwd, self.probe_exit))
        return ("no usable {0} was found for {1}; candidates tried: {2}".format(
            self.runner, self.cwd,
            ", ".join(self.candidates) if self.candidates else "none"))

    def payload(self) -> Dict[str, Any]:
        """The refusal, as typed fields. Every one of them has a reader:
        `reason` is branched on by callers and asserted by the tests,
        `candidates` and `probe_exit` are what an operator acts on, and
        `resolved` names the binary that failed (§3.6 B15)."""
        return {
            "outcome": RUNNER_UNUSABLE,
            "runner": self.runner,
            "reason": self.reason.value,
            "cwd": self.cwd,
            "candidates": list(self.candidates),
            "resolved": self.resolved,
            "probe_exit": self.probe_exit,
        }


@dataclass(frozen=True)
class ResolvedRunner:
    """One runner, resolved to an absolute invocation and proven capable.

    `launcher_args` is empty for a direct binary and carries the sub-command
    for an environment launcher — `uv run pytest` resolves to
    `executable=/…/uv`, `launcher_args=("run", "pytest")`. Without it the
    `uv`/`poetry` discovery ranks would be inexpressible and the precedence
    order in `resolve` would be a comment rather than code.
    """

    runner: str
    executable: str
    launcher_args: Tuple[str, ...] = ()
    origin: str = "declared"          # "declared" | "discovered"
    probe_exit: int = -1
    version: str = ""
    cwd: str = "."

    @property
    def argv_prefix(self) -> Tuple[str, ...]:
        return (self.executable,) + tuple(self.launcher_args)

    def collect_argv(self, gate: Any) -> Tuple[str, ...]:
        """The enumeration invocation for one gate. Never executes cases."""
        return (self.argv_prefix + COLLECT_ARGS[self.runner]
                + tuple(gate.argv))

    def execute_argv(self, argv: Sequence[str]) -> Tuple[str, ...]:
        """The execution invocation. `argv` is the gate's own argv tail: the
        binary *and the runner's own mode* are this object's to decide and
        never the caller's to supply.

        `EXECUTE_ARGS` is what makes the second half of that sentence true.
        Without it this returned `vitest <paths>`, which is watch mode, and a
        gate that never terminates is indistinguishable from a hung agent.
        """
        tail = tuple(argv)
        mode = EXECUTE_ARGS[self.runner]
        # An argv that already opens with the mode keeps exactly one copy of
        # it. The ingress strips `run` today, but a gate reaching here with it
        # still attached must execute, not fail on `vitest run run <paths>`.
        if mode and tail[:len(mode)] == mode:
            mode = ()
        return self.argv_prefix + mode + tail

    def record(self) -> Dict[str, Any]:
        return {
            "runner": self.runner,
            "executable": self.executable,
            "launcher_args": list(self.launcher_args),
            "origin": self.origin,
            "probe_exit": self.probe_exit,
            "version": self.version,
            "cwd": self.cwd,
        }

    def config_line(self) -> str:
        """The `runners:` entry that would make this resolution binding."""
        return "  {0}: {1}".format(
            self.runner, " ".join(self.argv_prefix))


# ── probing ─────────────────────────────────────────────────────────────────

def _run(argv: Sequence[str], cwd: Path, env: Optional[Mapping[str, str]],
         timeout_s: float) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            list(argv), cwd=str(cwd), env=dict(env) if env is not None else None,
            capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        # A probe that cannot be started is not capable, and the caller turns
        # that into a typed refusal. It is never an exception that escapes as
        # a stack trace into a lifecycle decision.
        return -1, ""
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def probe(runner: str, argv_prefix: Sequence[str], cwd: Path,
          env: Optional[Mapping[str, str]] = None,
          timeout_s: float = PROBE_TIMEOUT_S) -> int:
    """The probe's exit code, or `-1` when it could not be started."""
    args = PROBE_ARGS[runner]
    code, _ = _run(tuple(argv_prefix) + args, cwd, env, timeout_s)
    return code


def _version(argv_prefix: Sequence[str], cwd: Path,
             env: Optional[Mapping[str, str]]) -> str:
    _code, output = _run(tuple(argv_prefix) + VERSION_ARGS, cwd, env,
                         VERSION_TIMEOUT_S)
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def _measured(runner: str) -> bool:
    return runner in PROBE_ARGS and runner in CAPABLE_EXIT


# ── discovery ───────────────────────────────────────────────────────────────

def _rank_candidates(runner: str, repo: Path) -> List[List[Tuple[str, ...]]]:
    """Candidate invocations by falling precedence.

    Rank 1 is the repository's own environment, rank 2 and 3 are the two
    lockfile-driven launchers, and `PATH` is last rather than absent — a
    project that legitimately uses the system interpreter must still work, and
    the probe is what makes trusting it safe.
    """
    ranks: List[List[Tuple[str, ...]]] = []

    local: List[Tuple[str, ...]] = []
    for relative in (Path(".venv") / "bin" / runner,
                     Path("venv") / "bin" / runner,
                     Path("node_modules") / ".bin" / runner):
        candidate = repo / relative
        if _is_executable(candidate):
            local.append((str(candidate),))
    ranks.append(local)

    for launcher in ("uv", "poetry"):
        found = shutil.which(launcher)
        ranks.append([(str(Path(found).resolve()), "run", runner)]
                     if found else [])

    on_path = shutil.which(runner)
    ranks.append([(str(Path(on_path).resolve()),)] if on_path else [])
    return ranks


def _anchor_declared(value: str, repo: Path) -> Optional[Tuple[str, ...]]:
    """A declared runner value as an invocation, or `None` when unresolvable.

    Two modes, the same two `_resolve_binary` already has for `executables:`,
    plus a repository anchor: `.venv/bin/pytest` must resolve against the
    primary repository rather than against an attempt worktree, because the
    repository's environment is what the measurement in the module docstring
    shows working from a foreign cwd.
    """
    tokens = value.split()
    if not tokens:
        return None
    head, tail = tokens[0], tuple(tokens[1:])
    if "/" in head:
        candidate = (repo / head).resolve() if not Path(head).is_absolute() \
            else Path(head)
        return ((str(candidate),) + tail) if _is_executable(candidate) else None
    found = shutil.which(head)
    if found is None:
        return None
    resolved = Path(found).resolve()
    return ((str(resolved),) + tail) if _is_executable(resolved) else None


def resolve(runner: str, repo: Path, cwd: str = ".", *,
            declared: Optional[str] = None,
            env: Optional[Mapping[str, str]] = None,
            timeout_s: float = PROBE_TIMEOUT_S) -> ResolvedRunner:
    """The single producer. Raises `RunnerUnusable` rather than guessing.

    `cwd` is relative to `repo` and is where the probe runs, so capability is
    established in the directory the gate will actually collect from.
    """
    repo = Path(repo)
    working = (repo / cwd).resolve()
    if not _measured(runner):
        # No measured probe means capability cannot be established, and an
        # unproven runner is refused rather than trusted.
        raise RunnerUnusable(
            runner, Reason.UNRESOLVED, cwd,
            detail="{0} has no measured capability probe, so no invocation of "
                   "it can be proven able to collect; add its PROBE_ARGS and "
                   "CAPABLE_EXIT rows before declaring a {0} gate".format(
                       runner))
    if not working.is_dir():
        raise RunnerUnusable(
            runner, Reason.UNRESOLVED, cwd,
            detail="the gate's working directory does not exist: {0}".format(
                working))

    if declared is not None:
        invocation = _anchor_declared(declared, repo)
        if invocation is None:
            raise RunnerUnusable(
                runner, Reason.UNRESOLVED, cwd, candidates=(declared,),
                detail="runners.{0} is declared as {1!r}, which is not an "
                       "executable file".format(runner, declared))
        exit_code = probe(runner, invocation, working, env, timeout_s)
        if exit_code != CAPABLE_EXIT[runner]:
            # No fallback to discovery. A declaration that silently falls back
            # to a guess is not a declaration.
            raise RunnerUnusable(
                runner, Reason.INCAPABLE, cwd, candidates=(declared,),
                resolved=invocation[0], probe_exit=exit_code)
        return ResolvedRunner(
            runner=runner, executable=invocation[0],
            launcher_args=tuple(invocation[1:]), origin="declared",
            probe_exit=exit_code,
            version=_version(invocation, working, env), cwd=cwd)

    tried: List[str] = []
    for candidates in _rank_candidates(runner, repo):
        if not candidates:
            continue
        if len(candidates) > 1:
            raise RunnerUnusable(
                runner, Reason.AMBIGUOUS, cwd,
                candidates=[" ".join(item) for item in candidates])
        invocation = candidates[0]
        tried.append(" ".join(invocation))
        exit_code = probe(runner, invocation, working, env, timeout_s)
        if exit_code == CAPABLE_EXIT[runner]:
            return ResolvedRunner(
                runner=runner, executable=invocation[0],
                launcher_args=tuple(invocation[1:]), origin="discovered",
                probe_exit=exit_code,
                version=_version(invocation, working, env), cwd=cwd)
    raise RunnerUnusable(runner, Reason.UNRESOLVED, cwd, candidates=tried)


# ── the run record ──────────────────────────────────────────────────────────

def adoption_notice(resolved: Iterable[ResolvedRunner]) -> str:
    """The `runners:` block that would pin a discovered resolution.

    Printed whenever discovery decided, so adopting the declaration is the path
    of least resistance. What remains of the objection to discovery — that a
    silent pick can drift between runs — is answered by this notice plus the
    record below, not by making the upgrade a wall.
    """
    discovered = [item for item in resolved if item.origin == "discovered"]
    if not discovered:
        return ""
    lines = ["maestro resolved these gate runners by discovery; declare them "
             "in maestro.config.yaml to pin them:", "runners:"]
    lines.extend(sorted({item.config_line() for item in discovered}))
    return "\n".join(lines)


def write_record(path: Path, resolved: Iterable[ResolvedRunner]) -> None:
    """Persist every resolution beside the run's own artifacts.

    No ledger schema change: the run directory already holds `review/`,
    `scratch/`, and `worktrees/`, and one more sibling file fits that layout.
    Which interpreter executed stops being ambient the moment this is written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = sorted((item.record() for item in resolved),
                     key=lambda row: (row["runner"], row["cwd"]))
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
