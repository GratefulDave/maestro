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
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    NoReturn,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from . import handoff_budget as hb
from . import permissions
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
      — treating every failed launch as proven p pabsent — lies for the refusals
      raised *after* the split, where a pane may really exist and its group is
      exactly what quiescence is for. Reported rather than inferred, so the
      proof is skipped only where absence is established.

      For a post-split member the answer is not a property of the member at
      all: every such handler closes its own pane first, so whether a pane
      survives depends on whether that close succeeded. Those members declare
      `None` here and the raise site states the fact (`LaunchRefused(...,
      pane_created=...)`) after its cleanup has run. `None` with nothing
      stated falls back to `True`, which is the fail-closed answer.
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
    #: OMP cannot launch without the configured execution profile. Refused
    #: before any pane is created because the profile belongs to the immutable
    #: launch spec and retrying cannot supply it.
    OMP_PROFILE_REQUIRED = ("OMP_PROFILE_REQUIRED", False, True)
    #: The split returned without a pane id. §16.3 item 45 names this among
    #: the post-split refusals: herdr may hold a pane it did not report.
    NO_PANE = ("NO_PANE", None, False)
    #: The pane exists and never settled into an interactive shell.
    SHELL_NOT_READY = ("SHELL_NOT_READY", None, False)
    #: `agent start` refused the pane herdr had just handed us. Herdr's own
    #: precondition check is the authority on whether a pane can host an
    #: agent, and it answers with a typed `error.code` -- so the refusal is
    #: restated here rather than escaping as herdr's raw `HerdrCallError`,
    #: which is not a `LaunchRefused` and therefore says nothing about the
    #: pane the handler just closed.
    AGENT_START_REFUSED = ("AGENT_START_REFUSED", None, False)
    #: An agent started but its interactive composer or prompt submission did
    #: not complete. The launch path cancels that agent before reporting the
    #: refusal; another attempt may succeed.
    PROMPT_SUBMISSION_REFUSED = ("PROMPT_SUBMISSION_REFUSED", False, False)
    #: The pane herdr split is not bound to the worktree the spec named.
    #: Non-deterministic: the binding is herdr's to get right and another
    #: split may land correctly.
    BINDING_MISMATCH = ("BINDING_MISMATCH", None, False)
    #: The spec named a route this launcher cannot build an argv for. Refused
    #: before any pane is created, and deterministic because the route is a
    #: property of the immutable spec.
    UNSUPPORTED_ROUTE = ("UNSUPPORTED_ROUTE", False, True)
    #: The `herdr` binary could not be executed at all (missing, not
    #: executable, or an invalid argv). Deterministic: the configured
    #: executable does not change between attempts. `pane_created` is left
    #: to the raise site's fail-closed default because the same call surface
    #: is used before and after a pane exists; the callers that know no pane
    #: exists yet restate it. Previously a bare `RuntimeError` that no
    #: operator-facing clause mapped, so the operator saw a traceback.
    HERDR_UNAVAILABLE = ("HERDR_UNAVAILABLE", None, True)
    #: herdr could not say which pane this launcher splits from. Raised
    #: before any split, so no pane exists. **Non**-deterministic: the answer
    #: is herdr's to give and the next attempt may get it, and the launcher
    #: does not cache the failure, so a retry genuinely re-asks. This member
    #: replaced a silent fall back to the `--current` selector: a selector is
    #: mutable focus state, so splitting it lands the pane in whichever
    #: workspace happens to hold focus — the scatter across w13F..w13K — and
    #: §1.2 forbids keying a decision on ambient mutable state. Refusing is
    #: honest; splitting into an unknown workspace is not.
    SPLIT_PARENT_UNRESOLVED = ("SPLIT_PARENT_UNRESOLVED", False, False)
    #: herdr refused to create the workspace this run's panes belong to.
    #: Raised before any pane exists. **Non**-deterministic and not cached,
    #: for the same reason `SPLIT_PARENT_UNRESOLVED` is not: the answer is
    #: herdr's to give and the next attempt genuinely re-asks. Creating the
    #: workspace rather than reading whichever one holds focus is what makes
    #: a run's placement a property of the run instead of a property of where
    #: the operator's cursor happened to be (§1.2).
    WORKSPACE_UNRESOLVED = ("WORKSPACE_UNRESOLVED", False, False)
    #: herdr refused to create the tab this node's panes belong to. Raised
    #: before any pane of this launch exists; same non-deterministic,
    #: uncached shape as the workspace refusal above.
    TAB_UNRESOLVED = ("TAB_UNRESOLVED", False, False)
    #: The split landed outside the workspace this run is bound to. The pane
    #: exists by the time that is known, so it is reaped and the refusal
    #: states the reap. Non-deterministic: a split of a fixed parent lands
    #: beside it, so a drifting child is a fact about that split.
    WORKSPACE_DRIFT = ("WORKSPACE_DRIFT", None, False)
    #: B13: the prompt file will not fit the window the spec declared. Raised
    #: before any herdr call, so no pane exists. Deterministic: the prompt's
    #: size and the model's window are both properties of the spec, identical
    #: on every attempt, and a second launch would overflow exactly as far.
    PROMPT_TOO_LARGE = ("PROMPT_TOO_LARGE", False, True)
    #: B13's fail-closed half: the route publishes a catalog, so a window
    #: exists to be measured against, and the comparison could not be made --
    #: the spec carries no window, or the prompt file cannot be sized. Both
    #: sub-facts are named in the detail string rather than split into two
    #: members, because unlike D9's pair they do not lead to different
    #: decisions: neither can show the handoff fits, and B13 is that an
    #: unmeasured window is not a passing one.
    PROMPT_UNMEASURED = ("PROMPT_UNMEASURED", False, True)
    #: COMPLETE cleanup could not prove `/rename` confirmation. The pane stays
    #: open; publication is not rolled back. Non-deterministic: a retry may
    #: observe the confirmation that this attempt did not.
    SESSION_RENAME_UNCONFIRMED = ("SESSION_RENAME_UNCONFIRMED", True, False)


    def __init__(
        self, code: str, pane_created: Optional[bool], deterministic: bool
    ) -> None:
        self.code = code
        #: `None` means "the raise site must state it". A refusal raised after
        #: the split cannot answer this as a class constant: whether a pane
        #: still exists depends on whether the handler's own `pane close`
        #: succeeded, which is a fact about one attempt and not about the
        #: member. A constant here was wrong in the dangerous direction --
        #: every such handler closes the pane and then reported `True`, which
        #: sends §8.3's quiesce step after a group that was never registered.
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

    def __init__(
        self,
        refusal: LaunchRefusal,
        detail: str = "",
        pane_created: Optional[bool] = None,
    ) -> None:
        super().__init__(
            "LAUNCH_REFUSED:{}{}".format(refusal.code, ":" + detail if detail else "")
        )
        self.refusal = refusal
        self.detail = detail
        self._pane_created = pane_created

    @property
    def pane_created(self) -> bool:
        """Whether a pane was left behind, stated by whoever cleaned up.

        Three sources, in order. An explicit constructor argument wins,
        because only the raise site knows what its own cleanup achieved. A
        member that declares the fact by construction (nothing was split yet)
        answers next. Anything else fails closed at `True`: §8.3 refuses to
        report an absence nobody measured.
        """
        if self._pane_created is not None:
            return self._pane_created
        declared = self.refusal.pane_created
        return True if declared is None else declared

    @property
    def deterministic(self) -> bool:
        return self.refusal.deterministic


class HarnessCancelled(RuntimeError):
    """A harness-owned process was cancelled and its process group quiesced."""


class HarnessQuiescenceError(RuntimeError):
    """A harness-owned process group could not be proven absent."""


class _WorkspaceGone(RuntimeError):
    """A memoized workspace id no longer names a live workspace.

    Internal to the launcher and never raised past it: lane placement either
    recovers by re-resolving, or restates this as a typed `LaunchRefused`.
    A dead child invalidates that lane only; a dead parent invalidates every
    lane layout.
    """



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
#: `agent start` found the agent in the pane but it blocked during startup.
#: The agent is running and its name stays registered; the pane is owned.
AGENT_NOT_READY = "agent_not_ready"
PANE_NOT_FOUND = "pane_not_found"
WORKSPACE_NOT_FOUND = "workspace_not_found"

#: Maestro-owned Herdr report-metadata source. Tokens, not labels, are identity.
MAESTRO_METADATA_SOURCE = "maestro"
METADATA_TOKEN_KIND = "kind"
METADATA_TOKEN_RUN = "run_id"
METADATA_TOKEN_REPO = "repo"
METADATA_TOKEN_LANE = "lane"
METADATA_TOKEN_ROLE = "role"
METADATA_TOKEN_SCRATCH = "scratch"
METADATA_SCRATCH_REDIRECT = "redirect-v1"
METADATA_TOKEN_PARENT = "parent"
METADATA_KIND_RUN = "run"
METADATA_KIND_LANE = "lane"
METADATA_TOKEN_VALUE_MAX = 80


#: §8.3's cache-redirection variables, named here rather than only beside
#: `worktree.scratch_env`, because this module is where they cross the herdr
#: boundary. A variable absent from this tuple never reaches the agent's shell
#: however carefully it was computed.
#: `XDG_CACHE_HOME` is deliberately **not** among these, and its absence is a
#: fix rather than an omission. It was redirected here until 2026-08-27, when
#: `run-8d1a71f463e4430f92a125a8f8b3731d` spent twelve launcher attempts on a
#: tester lane whose agent came up `no-model`. A pane is forked by the herdr
#: server and builds its environment from a *login shell*, and a login shell
#: reads credentials through this variable -- on the machine that recorded the
#: incident, `~/.zshrc` sources `${XDG_CACHE_HOME:-$HOME/.cache}/keychain-
#: secrets.zsh`, which is what exports the provider API keys. Pointing it at a
#: fresh per-attempt scratch does not redirect a byproduct; it removes the
#: shell's own bootstrap, and the agent launches with no usable credentials and
#: a composer that will not accept a prompt.
#:
#: That is the asymmetry that makes this variable different from the six that
#: remain. `TMPDIR`, `PYTHONPYCACHEPREFIX`, `npm_config_cache`, the pytest
#: cache, `RUFF_CACHE_DIR` and `COVERAGE_FILE` name places a tool *writes*, so
#: redirecting them moves output. `XDG_CACHE_HOME` also names a place tools
#: *read*, and redirecting it hides input. §8.3's fence is about what lands in
#: the measured worktree, and nothing under `~/.cache` was ever inside one.
SCRATCH_ENV_KEYS: Tuple[str, ...] = (
    "TMPDIR",
    "PYTHONPYCACHEPREFIX",
    "PYTEST_ADDOPTS",
    "COVERAGE_FILE",
    "RUFF_CACHE_DIR",
    "npm_config_cache",
)

#: Only §8.3 scratch redirects are forwarded into the pane shell. `herdr agent
#: start` has no environment option; the pane inherits variables passed as
#: `--env` at tab/pane create.
PANE_ENV_KEYS: Tuple[str, ...] = SCRATCH_ENV_KEYS

ROLE_AGENT_DIR = ".maestro-agent"


def role_agent_dir(root: str | Path) -> Path:
    return Path(root).resolve() / ROLE_AGENT_DIR


def role_result_path(root: str | Path, turn: int) -> Path:
    return role_agent_dir(root) / "results" / "envelope-{}.json".format(turn)


def role_prompt_path(root: str | Path, turn: int) -> Path:
    return role_agent_dir(root) / "prompt-{}.json".format(turn)


def scratch_environment(root: str | Path) -> Dict[str, str]:
    """Create checkout-local scratch redirects for one role worktree."""
    base = role_agent_dir(root) / "scratch"
    redirects = {
        "TMPDIR": str(base / "tmp"),
        "PYTHONPYCACHEPREFIX": str(base / "pycache"),
        "PYTEST_ADDOPTS": "-o cache_dir={}".format(base / "pytest_cache"),
        "COVERAGE_FILE": str(base / "coverage"),
        "RUFF_CACHE_DIR": str(base / "ruff"),
        "npm_config_cache": str(base / "npm"),
    }
    for key in ("TMPDIR", "PYTHONPYCACHEPREFIX", "RUFF_CACHE_DIR", "npm_config_cache"):
        Path(redirects[key]).mkdir(parents=True, exist_ok=True)
    (base / "pytest_cache").mkdir(parents=True, exist_ok=True)
    (role_agent_dir(root) / "results").mkdir(parents=True, exist_ok=True)
    return redirects


def pytest_worker_cap(concurrency: int, cpu_count: Optional[int] = None) -> int:
    """Per-lane xdist workers: ``max(1, cores // concurrency)``.

    ``pytest.ini`` defaults to ``-n auto``. Six concurrent lanes each inheriting
    that on an 18-core box is 108 workers. A red final integration gate has no
    retry, so the oversubscription costs whole runs rather than minutes.
    """
    if concurrency < 1:
        raise ValueError("concurrency is ≥ 1")
    cores = os.cpu_count() if cpu_count is None else cpu_count
    if cores is None or cores < 1:
        cores = 1
    return max(1, int(cores) // concurrency)


def role_pane_environment(role_root: Path, base: Mapping[str, str]) -> Dict[str, str]:
    """Bind scratch redirects to one role checkout."""
    env = dict(base)
    env.update(scratch_environment(role_root))
    return env


def pane_env_flags_for_role(
    role_root: Path, environment: Mapping[str, str]
) -> Tuple[str, ...]:
    """Fail-closed `--env` flags bound to one role checkout."""
    return pane_env_flags(role_pane_environment(role_root, environment))


def pane_env_flags(environment: Mapping[str, str]) -> Tuple[str, ...]:
    """`--env KEY=VALUE` flags that actually reach the pane shell.

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
    Each precreated role pane must receive its own checkout's scratch paths;
    the launching role's mapping is not a substitute.

    Missing variables are refused rather than skipped. §8.3's preference order
    is redirect, then suppress, then the write convicts, and a redirect that
    silently fails to arrive convicts an agent for a harness defect. A refusal
    here happens before any untrusted code runs and names the variable.
    """
    missing = [key for key in PANE_ENV_KEYS if not environment.get(key)]
    if missing:
        raise LaunchRefused(LaunchRefusal.SCRATCH_REDIRECT_MISSING, ",".join(missing))
    flags: List[str] = []
    for key in PANE_ENV_KEYS:
        flags.extend(("--env", "{}={}".format(key, environment[key])))
    return tuple(flags)


#: The hard ceiling on one tab's grid, in both dimensions. Three columns of a
#: 170-column terminal are 57 columns each, which is still a pane an operator
#: can read; a fourth is not. Rows are capped with it so a tab degrades in
#: both dimensions together rather than in one of them without limit.
GRID_MAX = 3


def grid_for(pane_count: int) -> Tuple[int, int]:
    """`(rows, cols)` for `pane_count` panes in one tab, capped at 3x3.

    Rows first, columns from the rows, so the sequence widens before it
    stacks: 2 panes are two columns side by side, 3 are three columns, and
    only at 4 does a second row appear.

        1 -> 1x1    4 -> 2x2    7 -> 3x3
        2 -> 1x2    5 -> 2x3    8 -> 3x3
        3 -> 1x3    6 -> 2x3    9 -> 3x3

    Beyond nine the grid does not grow: `split_plan` keeps filling columns
    downward, so a tenth pane makes one column four cells tall rather than
    making a fourth column nobody could read. A tab is meant to hold fewer
    than six panes; nine is the ceiling, not the target.
    """
    count = max(1, int(pane_count))
    rows = min(GRID_MAX, -(-count // GRID_MAX))
    cols = min(GRID_MAX, -(-count // rows))
    return rows, cols


def split_plan(index: int, cols: int) -> Tuple[int, str]:
    """Which existing pane the `index`-th pane of a tab splits, and how.

    A grid is reachable by binary splits alone, with no pane ever moved, if
    each new pane names the right parent: the first row is built left to
    right by splitting the previous pane `right`, and every later pane splits
    the pane exactly one row above it -- `index - cols` -- `down`. Herdr
    rebalances the ratios itself, so an even grid needs no `--ratio`.

    That is what makes an up-front grid compatible with dispatch arriving one
    node at a time: the geometry is a pure function of the pane's ordinal and
    the tab's column count, so pane *k* can be placed correctly without
    knowing whether a pane *k+1* will ever exist. Only `cols` needs deciding
    in advance, and `LaunchSpec.pane_group_size` is where the caller declares
    it.
    """
    if index < 1:
        raise ValueError("GRID_INDEX_HAS_NO_PARENT")
    width = max(1, int(cols))
    if index < width:
        return index - 1, "right"
    return index - width, "down"


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
    #: Role-owned instructions materialized inside the assigned checkout.
    #: Launchers pass this file as an additive system prompt; the task prompt
    #: remains the per-turn JSON at ``prompt_path``.
    system_prompt_path: Optional[Path] = None
    environment: Mapping[str, str] = field(default_factory=dict)
    #: The target's published context window, in tokens, as measured by
    #: whoever owns the route's catalog. B13's size check is made against this
    #: at `HerdrLauncher.launch`, which is the one path every dispatched prompt
    #: passes through.
    #:
    #: Three values, three meanings, and the third is why this is not simply
    #: `int`: a positive number is a measured window; `0` says the catalog was
    #: read and carries no window for this model; `None` says nobody measured
    #: it. On a route in `ROUTES_PUBLISHING_A_WINDOW` the last two are both
    #: refusals — an unmeasured window is not a passing one — so leaving the
    #: field off cannot quietly disable the check for a new call site.
    #:
    #: Resolution lives at the CLI rather than here because reading a route's
    #: catalog means importing `agent_pi`, which `enforcement.py`'s
    #: `base-execution-import` forbids every module in this package to do.
    context_window_tokens: Optional[int] = None
    #: The authored plan name shown on the Herdr workspace. Runtime identity
    #: remains in `correlation_token`; it is never parsed for placement.
    workspace_label: str = ""
    #: Tab grouping key for panes of one lane. Not a session-adoption identity.
    lane_key: str = ""
    #: Authored lane name shown on the linked child worktree workspace.
    lane_label: str = ""
    pane_role: str = ""
    #: Durable lane coordinates used only to probe a pre-fix live agent.
    run_id: str = ""
    #: Canonical repository fingerprint for Herdr metadata identity.
    repository_fingerprint: str = ""
    #: Git repository parent working tree. Herdr `worktree open` requires this
    #: as the parent workspace cwd, not a linked role checkout.
    repository_root: Path = Path()
    stage: str = ""
    input_digest: str = ""
    attempt_no: Optional[int] = None
    #: How many agent panes the caller expects this tab to hold.
    pane_group_size: int = 0
    #: Per-role checkout roots used when a role pane is created lazily.
    role_cwds: Mapping[str, Path] = field(default_factory=dict)
    #: Refresh an adopted stable role's existing CWD before prompt delivery.
    prepare_adopted_cwd: Optional[Callable[[Path], None]] = None
    #: Optional callback once launch holds pane/actor ids. Transport only.
    on_identity: Optional[Callable[["LaunchHandle"], None]] = None



#: Pane status meaning the composer is not currently producing a turn.
AGENT_QUIESCENT_STATUS = "idle"

#: How long `AGENT_QUIESCENT_STATUS` must hold, with no transcript record
#: appearing, before `poll` reads it as a turn that stopped without declaring.
AGENT_QUIESCENCE_CONFIRM_S = 60.0


#: The statuses the recovery wait treats as "this round is over": the actor is
#: demonstrably alive (`working`), demonstrably settled after a turn (`done`),
#: or stopped at something it needs (`blocked`). Herdr 0.8.2's `agent wait
#: --until` accepts idle, working, blocked, done, unknown; the two omitted here
#: are omitted deliberately. `idle` is the state of a composer holding an
#: unsubmitted prompt, so waiting on it returns instantly and spends every
#: recovery Enter back-to-back. `unknown` describes an actor nobody can
#: describe, which is not a liveness signal.
RECOVERY_LIVENESS_STATUSES: Tuple[str, ...] = ("working", "done", "blocked")


#: NEVER let this begin with `/`. A leading slash opens Claude Code's
#: slash-command menu, and the Enter that follows is consumed ACCEPTING A
#: COMPLETION rather than sending the message -- observed 2026-08-27, where
#: `/team ...` was rewritten to `/oh-my-claudecode:team` and actually invoked
#: the OMC team skill, so the admission turn ran a slash command instead of
#: answering, spawned a teammate pane, and never wrote the reply marker the
#: capture waits for. The instruction reads identically with the verb named
#: mid-sentence, and nothing then triggers the menu.
#: How long to let a pasted prompt settle in the composer before pressing a
#: key at it. `pane send-text` returns when herdr has written the bytes, not
#: when the agent has taken them, so a key sent immediately after can land on
#: a composer that is still rendering and be swallowed.
PASTE_SETTLE_S = 2.0


CLAUDE_TEAM_PROMPT_PREFIX = (
    "Use `/team` to spawn subagents for tasks. If you get impatient do not "
    "duplicate "
    "the work yourself. Poll them. Make sure they use SendMessage to respond "
    "back to you. Operating rule: a teammate is idle only when it sent you a "
    "real SendMessage result, or `herdr agent list` no longer reports it as "
    "running. Silence, flat transcript mtime, and idle_notification are never "
    "evidence of idle -- never start its work yourself on those. "
)


def prepare_route_prompt_text(route: str, prompt: str) -> str:
    """Apply route-wide prompt policy to an arbitrary prompt."""
    if route != "claude" or prompt.startswith(CLAUDE_TEAM_PROMPT_PREFIX):
        return prompt
    return CLAUDE_TEAM_PROMPT_PREFIX + prompt


def prepare_route_prompt(spec: LaunchSpec) -> None:
    """Apply route-owned prompt text exactly once before size preflight."""
    text = spec.prompt_path.read_text(encoding="utf-8")
    prepared = prepare_route_prompt_text(spec.route, text)
    if prepared != text:
        spec.prompt_path.write_text(prepared, encoding="utf-8")


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
    #: The pane's foreground process group, resolved once the agent is running,
    #: for **liveness only** — `kill(pid, 0)`, never a signal. It is a separate
    #: field from `process_group` above rather than a way of finally filling it
    #: in, because the two are gated on different evidence: reading whether a
    #: group exists is safe under the recorded herdr surface, while §8.3's kill
    #: is conditional on a §9.8 receipt proving the group excludes the pane
    #: shell and every sibling attempt (§16.3 items 17 and 30). Merging them
    #: would let a fix for #20's read path silently arm a kill path no receipt
    #: covers. `None` means the group could not be told apart from the pane's
    #: own shell, and the attempt keeps exactly the two clocks it has today.
    liveness_pid: Optional[int] = None
    environment: Mapping[str, str] = field(default_factory=dict)
    #: Pane's Herdr workspace: the linked lane child for a run, else empty.
    workspace_id: str = ""
    tab_id: str = ""
    lane_key: str = ""
    #: The Space the lane child hangs under: the operator's own Space on the
    #: repository, or one Maestro created at the primary checkout. Never
    #: closed by Maestro. Empty for non-run launchers.
    parent_workspace_id: str = ""
    #: Linked child worktree workspace. Empty for non-run launchers.
    child_workspace_id: str = ""
    pane_role: str = ""
    lane_label: str = ""


    def __post_init__(self) -> None:
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )


@dataclass(frozen=True)
class PollResult:
    state: PollState
    exit_code: Optional[int] = None
    detail: str = ""


class LauncherAdapter(Protocol):
    def launch(self, spec: LaunchSpec) -> LaunchHandle: ...
    def poll(self, handle: LaunchHandle) -> PollResult: ...
    def cancel(self, handle: LaunchHandle, deadline: float) -> None: ...
    def classify(self, exc: BaseException) -> ErrorClass: ...
    def provision(self, worktree: Path) -> None: ...
    def resubmit(
        self,
        handle: LaunchHandle,
        prompt_path: Path,
        *,
        route: str = "",
        expected_token: Optional[str] = None,
        timeout_s: float = 60.0,
        envelope_path: Optional[Path] = None,
    ) -> LaunchHandle: ...
    def wait_for_idle(self, handle: LaunchHandle, timeout_s: float = 60.0) -> None: ...
    def retain(self, handle: LaunchHandle) -> None: ...
    def complete_run(
        self,
        handles: Sequence[LaunchHandle],
        *,
        project_identity: str = "",
        timeout_s: float = 60.0,
    ) -> None: ...



def preflight_launch_prompt(spec: LaunchSpec) -> Optional[int]:
    """B13 at the chokepoint: size-check the prompt file before dispatch.

    Returns the token estimate when the route publishes a window and the prompt
    fits, and `None` when the route publishes no window at all. Raises
    `LaunchRefused` otherwise, before any herdr call, so nothing is left behind.

    **Why here.** B13 was applied at three CLI dispatch sites, which is three
    places a fourth dispatch site does not have to visit. `HerdrLauncher.launch`
    is the single path every prompt this system dispatches actually takes — omp
    carries the prompt in argv (`build_omp_argv`), claude submits it into the
    composer — so a check here is the one that cannot be bypassed by adding a
    caller. The CLI's own check stays: refusing earlier gives the operator the
    failure at the site that assembled the oversized prompt.

    **What it measures.** `spec.prompt_path.stat().st_size`, an integer, against
    `spec.context_window_tokens`, an integer. Two integers and a ratio. Nothing
    opens the file and nothing reads a word of it, which is the form §1.2
    requires of anything that can stop a launch.

    **A route with no declared context window.** `claude` publishes no model
    catalog — `_deliver_author_turn` runs `opus`, which does not resolve in
    omp's catalog — so there is no number to compare against. The deliberate
    behaviour is to make no comparison and refuse nothing: a check that refused
    every dispatch on a windowless route would not be a size check, it would be
    a route that no longer launches. That is a statement about the route,
    recorded once in `handoff_budget.ROUTES_PUBLISHING_A_WINDOW`, and not an
    exemption a call site can claim for itself. When the claude route publishes
    a catalog, adding it to that tuple covers every dispatch on it at once.
    """
    if not hb.route_publishes_a_window(spec.route):
        return None
    window = spec.context_window_tokens
    if not window or window <= 0:
        raise LaunchRefused(
            LaunchRefusal.PROMPT_UNMEASURED,
            "{0}:no-window:{1}".format(spec.route, spec.model),
        )
    try:
        size = spec.prompt_path.stat().st_size
    except OSError as exc:
        raise LaunchRefused(
            LaunchRefusal.PROMPT_UNMEASURED,
            "{0}:unreadable-prompt:{1}".format(spec.route, exc),
        ) from exc
    estimate = hb.estimate_tokens_for_bytes(size)
    budget = hb.handoff_budget(window)
    if estimate > budget:
        raise LaunchRefused(
            LaunchRefusal.PROMPT_TOO_LARGE,
            "{0} bytes is ~{1} tokens against a {2}-token window (budget {3})".format(
                size, estimate, window, budget
            ),
        )
    return estimate


def build_omp_argv(binary: Path, spec: LaunchSpec) -> Tuple[str, ...]:
    """Build omp's host-agent argv without the node prompt.

    Recovered from 76861e2480afeaaba7b301ec14f055cc1f3911ed, then 1de318c
    additive ``--append-system-prompt``. Tool policy is the configured
    ``--profile``. Delegation is denied by that profile, not by a hook.
    Native Read/Write/Edit/Bash/skills/MCP stay available because argv does
    not pass a ``--tools`` allowlist or ``--hook``.
    """
    if not spec.profile:
        raise ValueError("OMP_PROFILE_REQUIRED")
    argv = [
        str(binary),
        "--profile",
        spec.profile,
        "--session-dir",
        str(spec.session_dir),
    ]
    if spec.system_prompt_path is not None:
        argv.extend(
            ("--append-system-prompt", str(spec.system_prompt_path.resolve()))
        )
    if spec.session_dir.is_dir() and any(spec.session_dir.glob("*.jsonl")):
        argv.append("-c")
    return tuple(argv)


def build_claude_argv(binary: Path, spec: LaunchSpec) -> Tuple[str, ...]:
    """Claude's host-agent argv from 76861e2 plus prompt-file and denylist.

    ``--disallowedTools`` is the native CLI control that denies Task/Agent.
    No ``--settings`` isolation hook and no container-wrapped Bash.
    """
    argv = [
        str(binary),
        "--model",
        spec.model,
        "--effort",
        spec.effort,
        "--dangerously-skip-permissions",
        *permissions.route_capability_argv(spec.route),
    ]
    if spec.system_prompt_path is not None:
        argv.extend(
            ("--append-system-prompt-file", str(spec.system_prompt_path.resolve()))
        )
    argv.append("--remote-control")
    return tuple(argv)


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
        complete = chunk[: boundary + 1]
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
        envelopes = [
            row for row in self._records if row.get("type") == "maestro_envelope"
        ]
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
        if not any(row.get("type") == "maestro_envelope" for row in self._records):
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
    argv: Sequence[str],
    *,
    cwd: Path,
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
        list(argv),
        cwd=str(cwd),
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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
            raise HarnessQuiescenceError("HARNESS_CONTEXT_QUIESCENCE_UNPROVEN") from exc
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


def agent_name_for(correlation_token: str) -> str:
    """The deterministic Herdr agent id persisted with one actor session."""
    return _agent_name(correlation_token)


PERSISTENT_PANE_ROLES = frozenset(
    {
        "tester",
        "test-reviewer",
        "builder",
        "code-reviewer",
        "integration-reviewer",
    }
)

LANE_PANE_ROLES: Tuple[str, ...] = (
    "tester",
    "test-reviewer",
    "builder",
    "code-reviewer",
    "integration-reviewer",
)

#: Internal pane_role -> operator-visible Herdr pane label.
PANE_ROLE_LABELS = {
    "tester": "tester",
    "test-reviewer": "tester-reviewer",
    "tester-reviewer": "tester-reviewer",
    "builder": "builder",
    "code-reviewer": "code-reviewer",
    "integration-reviewer": "integration-reviewer",
}

PANE_LABEL_ROLES = {
    "tester": "tester",
    "tester-reviewer": "test-reviewer",
    "test-reviewer": "test-reviewer",
    "builder": "builder",
    "code-reviewer": "code-reviewer",
    "integration-reviewer": "integration-reviewer",
}

#: Herdr 0.8.2 `AgentInfo.agent_status` is `idle | working | blocked | done |
#: unknown`. There is no `status` field and no dead/exited/stopped value: a
#: finished agent is `agent_not_found`, never a record with a dead status.
#: `blocked` and `unknown` are live-but-unobservable and refuse reconnection.
REUSABLE_AGENT_STATUSES = frozenset({"working", "idle", "done"})


def _agent_status_of(agent: Mapping[str, object]) -> str:
    return str(agent.get("agent_status") or "")


def _sanitize_project_identity(project_identity: str) -> str:
    return (
        "".join(
            ch if ch.isalnum() or ch in "-_." else "-"
            for ch in str(project_identity or "").strip()
        ).strip("-")
        or "maestro"
    )


def run_hash_prefix(run_id: str) -> str:
    """First four hash characters of a run id, stripping a leading ``run-``."""
    run = str(run_id or "").strip()
    if not run:
        raise ValueError("run_id is empty")
    if run[:4].lower() == "run-":
        run = run[4:]
    if not run:
        raise ValueError("run_id is empty")
    return run[:4]


def workspace_label_for(project_identity: str, run_id: str) -> str:
    """Caption for a parent Space Maestro has to create itself (no Space open
    on the repository): ``<basename>-<four-hash-chars>``. An operator's own
    Space keeps its own label."""
    return "{}-{}".format(
        _sanitize_project_identity(project_identity), run_hash_prefix(run_id)
    )


def pane_label_for(role: str) -> str:
    """Operator-visible pane label for a persistent role key."""
    key = str(role or "")
    return PANE_ROLE_LABELS.get(key, key)


def pane_role_for_label(label: str) -> str:
    """Internal pane_role for an operator-visible pane label, or ``""``."""
    return PANE_LABEL_ROLES.get(str(label or ""), "")


def session_name_for(
    project_identity: str, run_id: str, lane_id: str, role: str
) -> str:
    """OMP/Claude ``/rename`` target before COMPLETE cleanup closes the pane."""
    return "{}-{}-{}".format(
        workspace_label_for(project_identity, run_id),
        str(lane_id or ""),
        pane_label_for(role),
    )


def session_rename_confirmation(session_name: str) -> str:
    """Exact composer confirmation required before a pane may close."""
    return 'Session renamed to "{}".'.format(session_name)


def git_primary_workdir(repo: Path) -> Path:
    """The Git repository parent working tree, never a linked worktree cwd."""
    path = Path(repo).resolve()
    result = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    common = Path((result.stdout or "").strip())
    if result.returncode == 0 and common.name == ".git":
        return common.parent
    return path


def role_session_token(run_id: str, lane_id: str, role: str) -> str:
    """Stable correlation token for one persistent role session."""
    return "{}:{}:{}".format(run_id, lane_id, role)


def _herdr_tokens(item: Mapping[str, object]) -> Dict[str, str]:
    raw = item.get("tokens")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _metadata_token_flags(tokens: Mapping[str, str]) -> Tuple[str, ...]:
    """`--token k=v` flags for `report-metadata`; refuses an unwritable token.

    An over-long value used to be dropped silently, which tagged the object
    with an incomplete identity and turned it into a label-only workspace that
    every later lookup refuses. Identity is refused before it is written, and
    the callers validate before creating the object the tokens describe.
    """
    flags: List[str] = []
    for name, value in tokens.items():
        text = str(value or "")
        if not text:
            continue
        if len(text) > METADATA_TOKEN_VALUE_MAX:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "METADATA_TOKEN_TOO_LONG:{}:{}".format(name, len(text)),
                pane_created=False,
            )
        flags.extend(("--token", "{}={}".format(name, text)))
    return tuple(flags)


def _tokens_match(actual: Mapping[str, str], expected: Mapping[str, str]) -> bool:
    for key, value in expected.items():
        if not value:
            return False
        if actual.get(key) != value:
            return False
    return True



def _herdr_label(item: Mapping[str, object]) -> str:
    return str(item.get("label") or item.get("name") or "")


def _same_resolved_path(reported: object, expected: Path) -> bool:
    """Whether a Herdr-reported path names `expected` after resolution."""
    text = str(reported or "")
    if not text:
        return False
    return Path(text).resolve() == Path(expected).resolve()


def _herdr_list(payload: Mapping[str, object], key: str) -> List[dict]:
    result = payload.get("result", payload)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    value = result.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    nested = result.get("result")
    if isinstance(nested, dict):
        value = nested.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


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


def _agent_record(payload: Mapping[str, object]) -> Optional[dict]:
    """The agent object from `agent get` / `agent wait`, however Herdr nests it.

    Successful `agent get <name>` is identity. The record may omit `name`;
    requiring that field made live adoption fail and spawned duplicate agents.
    """
    agent = _extract(payload, "agent")
    if isinstance(agent, dict):
        return agent
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return None
    if (
        result.get("pane_id")
        or result.get("agent_status")
        or result.get("status")
        or result.get("agent_session")
    ):
        return result
    return None


def _agent_named(agent: Mapping[str, object], name: str) -> bool:
    reported = agent.get("name")
    if reported is None or reported == "":
        return True
    return str(reported) == name


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
    if None not in (shell_pid, pgid, proc_pid) and not (shell_pid == pgid == proc_pid):
        return False
    token = str(proc.get("name") or proc.get("argv0") or "")
    argv = proc.get("argv")
    if isinstance(argv, list) and argv:
        token = token or str(argv[0] or "")
    base = token.rsplit("/", 1)[-1].lstrip("-").lower()
    return base in ("zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh")


def pane_liveness_pid(herdr_call: Callable[..., dict], pane_id: str) -> Optional[int]:
    """The pane's foreground process group, for asking whether it still exists.

    `herdr pane process-info` reports the pane's foreground group, which is
    the agent while the agent is running and the pane's own shell otherwise.

    **This value is for `kill(pid, 0)` and nothing else.** It is deliberately
    not written to `LaunchHandle.process_group`, and the difference is the
    whole of the judgement here. That field is §8.3's kill target, and §8.3
    conditions writing it on an executed §9.8 receipt proving the group
    excludes the pane shell and every sibling attempt (§16.3 items 17 and 30).
    No such receipt exists. Asking whether a group is alive and sending it
    SIGKILL fail in opposite directions: a wrong answer here reports a live
    attempt dead or a dead one live, and the design already survives the
    latter because it is today's behaviour; a wrong answer on the kill path
    terminates the operator's shell.

    Returns `None` rather than guessing whenever the payload does not show a
    foreground group distinct from the pane's shell:

    * `_available_shell` is true — the foreground is a lone interactive shell,
      so the agent has not started or has already exited. Recording the
      shell's group would make PROCESS_DEAD permanently unreachable *and*
      permanently silent, which is worse than the gap it replaces because it
      would look fixed.
    * the group equals `shell_pid`, the same case reached without the
      process-name evidence `_available_shell` needs.
    * the call fails, or the payload carries no group.

    A `None` return leaves the attempt exactly where it is today: convicted by
    the turn clock and the node clock, never by absence.
    """
    try:
        payload = herdr_call("pane", "process-info", "--pane", pane_id)
    except RuntimeError:
        return None
    info = _extract(payload, "process_info")
    if not isinstance(info, dict):
        return None
    pgid = _optional_int(info.get("foreground_process_group_id"))
    if pgid is None or pgid <= 0:
        return None
    if pgid == _optional_int(info.get("shell_pid")):
        return None
    if _available_shell(payload):
        return None
    return pgid


def _is_text_read(args: Sequence[str]) -> bool:
    """`herdr agent read` / `herdr pane read` print the snapshot as raw text.

    They have no JSON output mode (`--format` accepts only `text` and `ansi`),
    so a JSON decode failure on those commands is the normal case, not a
    protocol violation.
    """
    return len(args) >= 2 and args[0] in ("agent", "pane") and args[1] == "read"


def _agent_transcript_path(
    agent: object,
    launched_cwd: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Resolve the transcript Herdr identifies for an agent.

    Herdr reports JSONL-writing routes as ``agent_session.kind == "path"``.
    Claude remote-control sessions instead report a typed session ``id`` even
    though Claude writes the ordinary project JSONL under its config root. The
    ID is not itself a path, so resolve it only for Herdr's Claude source and
    only when the exact cwd-derived file exists.
    """
    if not isinstance(agent, dict):
        return None
    direct = agent.get("transcript_path")
    if direct:
        return Path(str(direct))
    session = agent.get("agent_session")
    if not isinstance(session, dict):
        return None
    value = session.get("value")
    if not value:
        return None
    if session.get("kind") == "path":
        return Path(str(value))
    if (
        session.get("kind") != "id"
        or session.get("source") != "herdr:claude"
        or launched_cwd is None
    ):
        return None
    supplied = environment or {}
    config_root = supplied.get("CLAUDE_CONFIG_DIR") or os.environ.get(
        "CLAUDE_CONFIG_DIR"
    )
    root = Path(config_root).expanduser() if config_root else Path.home() / ".claude"
    project = "".join(
        character if character.isalnum() or character == "-" else "-"
        for character in str(launched_cwd.resolve())
    )
    candidate = root / "projects" / project / (str(value) + ".jsonl")
    return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class SubmitCallFailure:
    """One herdr call the submission path absorbed instead of raising.

    The recovery loop deliberately survives individual call failures — a pane
    mid-repaint refuses a key it would take a moment later — but surviving a
    failure and discarding it are different acts. On 2026-08-27 every
    `agent send-keys` in the recovery loop failed on the herdr agent-registry
    lookup for a just-registered admission agent, each failure was swallowed,
    and the loop refused `AGENT_PROMPT_UNSUBMITTED` — a statement about the
    composer that was actually a statement about a name lookup, and nothing in
    the refusal could say so.

    Typed record, diagnostic only: it travels on the refusal so the caller can
    report *which* call failed and how, and per §1.2 nothing keys a lifecycle
    transition on it. `code` is Herdr's own `error.code` when the failure
    carried one, else ``""``.
    """

    phase: str
    argv: Tuple[str, ...]
    error: str
    code: str


def _swallowed_code(exc: BaseException) -> str:
    """Herdr's typed `error.code` for an absorbed failure, or `""`.

    Reads the `.code` field both `HerdrCallError` and route admission's
    `AdmissionError` carry, then falls back to walking the `from` chain.
    """
    code = getattr(exc, "code", "")
    if isinstance(code, str) and code:
        return code
    return _herdr_error_code_of(exc)


def _swallowed_summary(failures: Sequence["SubmitCallFailure"]) -> str:
    """Compact operator-facing rendering of the absorbed failures.

    Appended to refusal messages so the report survives the `str(exc)`
    restatements between here and the attempt record. Callers must not branch
    on it (§7.5); the typed tuple on the refusal is the record.
    """
    if not failures:
        return ""
    order: List[str] = []
    counts: Dict[str, int] = {}
    for failure in failures:
        key = "{0}:{1}".format(failure.phase, failure.code or failure.error)
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += 1
    return " swallowed=[{0}]".format(
        ", ".join("{0}x{1}".format(key, counts[key]) for key in order)
    )


class PromptNotSubmitted(RuntimeError):
    """The composer holds the prompt text and will not submit it.

    Raised only after every recovery attempt has been spent *and* the pane's
    revision counter was legible throughout, so it means the pane is genuinely
    wedged rather than merely slow or merely unreadable. `failures` carries
    every absorbed call failure (`SubmitCallFailure`) observed along the way —
    evidence for the report, never an input to a transition (§1.2).
    """

    def __init__(
        self,
        message: str,
        failures: Sequence[SubmitCallFailure] = (),
    ) -> None:
        super().__init__(message)
        self.failures: Tuple[SubmitCallFailure, ...] = tuple(failures)


class PromptSubmissionUnobservable(RuntimeError):
    """The pane's revision counter could not be read, so nothing was proven.

    D9. "The meter did not move" and "I could not read the meter" are
    different facts and must not collapse to the same terminal outcome. Both
    fail closed -- neither ever reports a prompt as submitted -- but only the
    first is a statement about the prompt. The second is a statement about
    herdr: a `pane get` that timed out, a server mid-restart, a pane record not
    yet published. A single transient read failure at baseline capture used to
    guarantee `AGENT_PROMPT_UNSUBMITTED` even when the prompt had landed
    perfectly, which spends the attempt and then the node's whole retry budget
    on a launcher that was never broken.

    `classify_error` maps this to the existing `TRANSIENT` class -- no new
    retry class -- so the scheduler retries the launch instead of burning it.

    The same distinction covers the other two observation channels. A call
    that never *delivered* a single Enter (`AGENT_PROMPT_UNDELIVERED`) has no
    fact about the composer either — it pressed nothing, so "the composer will
    not submit" was never established — and a `submission_recorded` predicate
    that raised on every consultation observed nothing about the transcript.
    Both are statements about herdr or the proof channel, both fail closed,
    and both carry their absorbed failures in `failures`.
    """

    def __init__(
        self,
        message: str,
        failures: Sequence[SubmitCallFailure] = (),
    ) -> None:
        super().__init__(message)
        self.failures: Tuple[SubmitCallFailure, ...] = tuple(failures)


#: How many times to press Enter on an unsubmitted composer before giving up.
#: More than one because a single fallback is what failed in the recorded
#: incident: the Enter went in while the composer was still not accepting
#: input, the one verification wait then blocked for the whole remaining
#: budget, and the 600s window expired around a prompt that was never sent.
SUBMIT_ATTEMPTS = 4


def pane_revision(
    herdr_call: Callable[..., dict],
    pane_id: str,
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> Optional[int]:
    """The pane's monotonic revision counter, or None when it cannot be read.

    Diagnostic only. §1.2 permits reading this typed integer, but it cannot
    prove a prompt was submitted: pasting the text is itself a repaint, so the
    counter advances whether the composer accepted the turn or is still holding
    it. `agent_status` cannot either — a pane that never accepted the prompt
    and a pane whose short turn already finished both report `idle`.

    ``None`` is the typed answer for "unreadable" (D9); `on_error` receives
    the failure that produced it, so an eventual `AGENT_PROMPT_UNOBSERVED`
    refusal can say *why* the meter was unreadable instead of only that it was.
    """
    try:
        payload = herdr_call("pane", "get", pane_id, timeout=15.0)
    except Exception as exc:
        if on_error is not None:
            on_error(exc)
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
    attempts: int = SUBMIT_ATTEMPTS,
    working_proves: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    refuse_unproven: bool = True,
    submission_recorded: Optional[Callable[[], bool]] = None,
) -> None:
    """Offer one prompt to an agent composer and press it until it takes.

    **`herdr agent prompt` is not used here, and reviving it reinstates a
    known production stall.** That verb submits an encoded Enter atomically
    with the text, and `@` opens the composer's file-path completion popup in
    both omp and Claude Code -- while that popup is open the Enter is consumed
    ACCEPTING A COMPLETION rather than sending the message. Measured against a
    live omp composer on 2026-08-27, and observed in production the same day
    with a grok builder holding `@<prompt>` unsubmitted for an hour until an
    Enter typed by hand started the turn. The sequence that ships instead is
    three separate calls -- `pane send-text`, `pane send-keys esc`, `pane
    send-keys enter` -- with a settle between them; see the offer below.

    `pane run` is documented for ordinary terminals, servers, and shells, so
    pointing it at a pane hosting a coding agent types the prompt as a shell
    command instead of handing it to the agent composer. It stays refused.

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

    **`refuse_unproven` decides whether the end of the budget is a verdict.**
    A bounded look at an unbounded quantity must not produce a terminal
    refusal. Turn length is unbounded -- §7.6 measured omp writing its
    transcript at TURN granularity, 57.7s in the measured case, and states
    plainly that a turn doing real work runs far longer -- so no window over
    "has the transcript recorded this submission yet" can ever be sized
    correctly, and `AGENT_PROMPT_UNSUBMITTED` at the end of one is a statement
    about Maestro's clock wearing the costume of a statement about the
    composer. On 2026-08-27, run-8a200af7f9044ce7a11a51b6908f37e3's
    lane-wp6-tests attempt a4 had its transcript hit disk carrying the marker
    at 11:44:22 and was refused at 11:44:22.707; the paste had worked, and the
    refusal cancelled the handle.

    So the *lane* path passes `refuse_unproven=False`: the Enters are pressed,
    the launch identity is written, and the function returns "offered,
    unproven" rather than convicting. Adjudication then belongs to the node's
    liveness and quiescence machinery, which is already wall-clock-free and
    already arms only after `working` or `blocked` is observed (B14). Route
    admission keeps the default `True` -- see its call site for why its turn is
    bounded by construction.

    `AGENT_PROMPT_UNDELIVERED` (herdr accepted no Enter at all, so nothing
    was ever pressed) still raises on both paths: that is not a claim about
    turn length.

    `AGENT_PROMPT_UNOBSERVED` (every consultation of the proof channel
    raised) stays fail-closed on the bounded admission path
    (`refuse_unproven=True`). The lane path cannot size a window over an
    unbounded turn, so a dead proof channel there is offered-unproven:
    absorbed probe failures stay diagnostic and the node's liveness and
    quiescence machinery adjudicates.

    `monotonic` is injected for the same reason `sleep` is. The grace deadline
    below reads a clock, and a fake that cannot move that clock cannot express
    "the transcript flushes one turn later" without literally waiting a turn --
    which made the whole failure above untestable (§16.3 item 62: a fake that
    cannot express the failure is not coverage).
    """
    target = agent_name or pane_id
    total_s = max(5.1, max(0.001, timeout_s))
    # Every call failure this function survives is recorded here rather than
    # discarded. Surviving a failure is deliberate — the pane may be
    # mid-repaint — but a discarded failure made the 2026-08-27 refusal claim
    # the composer was wedged when in fact every recovery Enter had died on an
    # agent-registry lookup and nothing had been pressed at all. The record
    # travels on the refusal; nothing branches on it (§1.2).
    failures: List[SubmitCallFailure] = []
    # How many Enter keypresses herdr actually accepted. Zero at refusal time
    # means "the composer will not submit" was never tested, only asserted.
    enters_delivered = 0
    # Whether `submission_recorded` ever answered without raising. A predicate
    # that raised on every consultation observed nothing about the transcript,
    # which is D9's distinction applied to the proof channel.
    proof_observed = False

    def absorb(phase: str, argv: Tuple[str, ...], exc: BaseException) -> None:
        failures.append(
            SubmitCallFailure(
                phase=phase,
                argv=argv,
                error=type(exc).__name__,
                code=_swallowed_code(exc),
            )
        )

    # Diagnostic meter only. A revision advance does not prove consumption:
    # paste itself repaints. Readings distinguish Unobservable from
    # NotSubmitted after recovery is spent. `working` is not proof here either.
    baseline = pane_revision(
        herdr_call,
        pane_id,
        on_error=lambda exc: absorb("meter-read", ("pane", "get", pane_id), exc),
    )
    # Every legible reading taken *after* the prompt was offered. Emptiness is
    # the structural fact D9 turns on: it says the meter was never readable,
    # which is not the same claim as "the meter did not move".
    readings: List[int] = []
    # The recovery wait ends the round the moment the actor is demonstrably
    # alive OR demonstrably settled; see `RECOVERY_LIVENESS_STATUSES` for why
    # `idle` and `unknown` are not in that set. It was `("working",)` alone
    # until 2026-08-27, which made the wait raise on two entirely different
    # facts -- a composer that never took the prompt, and a turn that had
    # already finished (`done`) or stopped at a permission prompt (`blocked`)
    # before the wait was even issued. A fast accepted turn is still proven by
    # a rising transcript record, never by this wait returning.
    recovery_until = RECOVERY_LIVENESS_STATUSES

    def consumed(revision_only: bool = False) -> bool:
        """Whether the actor proves it accepted the offered prompt.

        `idle` is worthless: a composer holding an unsubmitted `@<path>` also
        reports idle. A pane revision advance is a repaint, not a turn — never
        positive proof. Missing, malformed, or any other live signal remains
        unproven unless an explicit predicate or the Claude working fallback
        says otherwise.

        Positive proof is `submission_recorded` when the caller supplied one
        and it returns true. Typed `working` remains a fallback for Claude
        (admission folds it into that predicate; the no-transcript path here
        consults it only when the meter is unreadable). `revision_only`
        drops the status half at the stalled-offer read, before Enter.
        """
        nonlocal proof_observed
        current = pane_revision(
            herdr_call,
            pane_id,
            on_error=lambda exc: absorb("meter-read", ("pane", "get", pane_id), exc),
        )
        if current is not None:
            readings.append(current)
        # The durable transcript record is the ONLY positive proof whenever the
        # caller can supply one, and the pane revision is then diagnostic.
        #
        # A revision cannot carry this proof at all. Pasting the prompt
        # repaints the pane, so the counter advances by the same +1 whether the
        # composer accepted the turn or is merely rendering text it is still
        # holding. On 2026-08-27 a grok builder sat at revision 1 with
        # `@<prompt>` unsubmitted in its composer while this function, reading
        # 0 -> 1, returned success without pressing a single Enter; the node
        # blocked `ENVIRONMENTAL_BUDGET_EXHAUSTED` with receipt `NEVER_STARTED`.
        # One Enter afterwards started the turn and drove that same counter past
        # 1000 within four seconds. The counter is a repaint meter, not a turn
        # meter, and no threshold over it separates the two cases.
        if submission_recorded is not None:
            try:
                answer = bool(submission_recorded())
            except Exception as exc:
                # Fail closed, but keep the failure: a proof probe that dies
                # is a missing observation about the transcript, not a False
                # about the prompt, and the refusal must be able to say so.
                absorb("proof-probe", (), exc)
                return False
            proof_observed = True
            return answer
        # The 2026-08-27 meter fallback (`current > baseline`) is the defect
        # this function used to return True through. It does not return here.
        #
        # `working` was trusted beside a legible counter the same day, when a
        # reviewer on the `claude` route sat at revision 1 holding
        # `@<prompt>.md` in its composer while the launcher returned a handle
        # and the finalization window waited forty minutes for a report no
        # actor was writing. So where the counter can be read, working does
        # not outrank it. Claude's composer can accept a prompt without
        # moving the meter at all: typed working remains the fallback when
        # the meter is unreadable, and herdr's field is `agent_status`
        # (`status` is always None).
        meter_unreadable = baseline is None or current is None
        if not revision_only and working_proves and agent_name and meter_unreadable:
            try:
                payload = herdr_call("agent", "get", agent_name)
            except Exception as exc:
                absorb("status-probe", ("agent", "get", agent_name), exc)
                return False
            agent = _agent_record(payload)
            if (
                isinstance(agent, dict)
                and (agent.get("agent_status") or agent.get("status")) == "working"
            ):
                return True
        return False

    def wait_for(budget_s: float) -> bool:
        """Give the composer a budget, then read the meter — always read it.

        The wait is a *delay*, never the observation. Returning on its failure
        without reading is what made D9's distinction unreachable on the only
        path that needs it: `agent wait --until working` raises whenever the
        agent does not reach `working` inside the budget, which is precisely
        the stalled-composer case *and* the fast-turn case, so every round
        pressed Enter and looked at nothing. `readings` then stayed empty and
        the caller reported "the meter could not be read" about a meter it had
        never read after offering the prompt — deterministically, on every
        attempt, until the node's whole launcher budget was gone.
        """
        recovery_argv: List[str] = []
        for status in recovery_until:
            recovery_argv.extend(["--until", status])
        argv = [
            "agent",
            "wait",
            target,
            *recovery_argv,
            "--timeout",
            str(int(budget_s * 1000)),
        ]
        started = monotonic()
        try:
            herdr_call(*argv, timeout=budget_s + 5.0)
        except Exception as exc:
            # Expected for both the stalled composer and the fast turn — the
            # wait raising is not the observation, `consumed()` below is. It
            # is still recorded: a wait that dies on a name lookup instead of
            # a timeout burns its round in milliseconds, and only the record
            # makes that visible in the refusal.
            absorb("recovery-wait", tuple(argv), exc)
        if consumed():
            return True
        # The round has a floor, and the wait is not it.
        #
        # An Enter is only worth pressing at a composer that has had a moment
        # to do something with the last one. Until 2026-08-27 that moment was a
        # side effect of `--until working` always timing out, so widening the
        # state set — the fix directly above — would have collapsed every round
        # into a back-to-back keystroke for any actor herdr reports `done` or
        # `blocked`, which is the 2026-08-18 shape all over again.
        #
        # The floor is `PASTE_SETTLE_S`, not the round budget, because it is
        # the same measured quantity: how long the composer needs to take what
        # was sent it before the next key can land. A wait that blocked for its
        # whole budget has already spent far more than that and sleeps nothing
        # here. Spacing is bounded by construction — a gap between two key
        # presses — which is what separates it from turn length and keeps this
        # from being another invented clock.
        remaining = min(budget_s, PASTE_SETTLE_S) - (monotonic() - started)
        if remaining > 0:
            sleep(remaining)
            return consumed()
        return False

    # Deliver the text and the Enter as two separate calls, never as
    # `agent prompt`'s atomic text-plus-Enter.
    #
    # `agent prompt` submits the encoded Enter in the same breath as the text,
    # and `@` opens the composer's file-path completion popup in both omp and
    # Claude Code. While that popup is open it consumes Enter to accept a
    # completion rather than to send the message, so the Enter is eaten and the
    # prompt sits composed and unsent -- the 2026-08-27 stall, where a grok
    # builder held `@<prompt>` for an hour and one Enter typed by hand started
    # the turn at once.
    #
    # Measured against a live omp pane: `pane send-text` followed by a separate
    # `pane send-keys enter` submits reliably, because the popup has settled
    # before the Enter arrives. The same probe showed the pane revision sitting
    # at 1 while the agent was demonstrably `Working`, which is why submission
    # is proven from the transcript and never from the meter.
    stalled = False
    try:
        herdr_call("pane", "send-text", pane_id, text, timeout=total_s + 5.0)
    except Exception as exc:
        # Only a stall falls through. Anything else -- a refused pane, a dead
        # socket, a configuration error -- is a real failure about the launch
        # and must propagate rather than be retried as if the composer had
        # merely swallowed the text; retrying those burns the attempt budget
        # against a condition no Enter can fix.
        if "agent_prompt_stalled" not in str(exc):
            raise
        stalled = True
    # Close the completion popup BEFORE offering the Enter.
    #
    # `@` opens omp's file-path completion popup and the popup keeps focus
    # after the path is fully typed -- measured 2026-08-27 against a live omp
    # composer, which rendered `hosts` / `hosts.equiv` under `@/etc/hosts` and
    # then consumed the next Enter to accept a completion rather than to
    # submit. The text stayed on screen, the pane revision did not move, and
    # the turn never started: exactly run-faa4dc49's
    # lane-routing-chemical-tests, whose composer held the prompt for minutes.
    #
    # The same probe showed `esc` closes the popup and leaves the composed
    # text intact, after which a single Enter submits. So press it first and
    # unconditionally: with no popup open `esc` is inert against a composer
    # that is not mid-turn, and pressing it costs one keystroke against a
    # failure that costs the whole attempt.
    #
    # Sending two Enters instead would also submit, but only by accident of
    # the popup eating the first: against a route with no popup the second
    # Enter lands on an empty composer as a stray blank turn.
    #
    # LET THE PASTE LAND FIRST. `send-text` returns when herdr has written the
    # bytes to the terminal, NOT when the composer has taken them, and these
    # three calls otherwise fire within milliseconds of each other. A Claude
    # pane that has not committed the paste yet swallows the Enter and holds
    # the text: measured 2026-08-27, where the identical sequence with ~2s
    # between steps submitted on the first Enter (`status done`, revision
    # 1 -> 2) while the back-to-back version left the same prompt composed and
    # unsent, `0 tokens`, through every retry round. The wait is the fix; the
    # keys were always right.
    sleep(PASTE_SETTLE_S)
    try:
        herdr_call("pane", "send-keys", pane_id, "esc", timeout=30.0)
    except Exception as exc:
        # A refused `esc` is not a failed submission. Record it and fall
        # through to the Enter, which is the thing that actually decides.
        absorb("popup-esc", ("pane", "send-keys", pane_id, "esc"), exc)
    sleep(PASTE_SETTLE_S)
    try:
        herdr_call("pane", "send-keys", pane_id, "enter", timeout=30.0)
        enters_delivered += 1
    except Exception as exc:
        absorb("offer-enter", ("pane", "send-keys", pane_id, "enter"), exc)
        stalled = True
    # Read the meter whether or not the offer stalled. A stall is herdr saying
    # it did not *observe* a lifecycle change inside its five-second floor,
    # which is not a statement that the composer refused the text: an agent
    # that accepts and finishes within that window stalls the wait and
    # advances the revision all the same. Reading only on the success branch
    # spent a recovery round on a prompt that had already landed — and, with
    # the round's own read skipped too, left the whole call with no reading at
    # all to reason from.
    if consumed(revision_only=stalled):
        return

    rounds = max(1, attempts)
    # Kept above herdr's own five-second lifecycle-observation floor, or the
    # verify degrades into a plain timeout that proves nothing.
    per_round = max(5.1, total_s / rounds)
    for round_no in range(rounds):
        # Verify BEFORE pressing, never after.
        #
        # The loop pressed first and looked afterwards, which means every
        # round typed a key into a pane that may already have been working:
        # the transcript record this function proves submission from is
        # written as the turn starts, so an agent that accepted the prompt on
        # the previous key is still indistinguishable from a stalled one until
        # someone waits and reads. Waiting first makes the extra Enter land
        # only on a composer that has demonstrably done nothing, and a prompt
        # that did go through costs zero further keystrokes.
        if wait_for(per_round):
            return
        try:
            # NO `esc` here, ever. In omp `esc` is the interrupt key -- the
            # composer renders `Working... <esc>` while a turn runs -- so an
            # `esc` pressed after the prompt landed kills the turn it was
            # meant to rescue. Pressing it every recovery round is what made
            # run-c672e173's attempts 1 and 2 run nine turns each and then
            # die `ENVIRONMENTAL` with the agent stopped mid-work.
            #
            # `esc` closes the completion popup exactly once, before the first
            # Enter, at a moment when no turn can be running because nothing
            # has been submitted yet. After that the only safe key is Enter.
            herdr_call("agent", "send-keys", target, "enter", timeout=30.0)
            enters_delivered += 1
        except Exception as exc:
            # A send-keys that fails on one round is not fatal: the pane may
            # be mid-repaint. But the failure is evidence, not noise — on
            # 2026-08-27 `agent send-keys` failed the herdr agent-registry
            # lookup for a just-registered admission agent on EVERY round,
            # each failure was discarded, and the refusal then claimed the
            # composer had swallowed four Enters that were never delivered.
            absorb("recovery-enter", ("agent", "send-keys", target, "enter"), exc)
            # `agent_not_found` is Herdr's typed statement that the
            # agent-scope verb cannot resolve this target at all, so pressing
            # it again through the registry can never deliver. `pane
            # send-keys <pane_id>` is the scope measured to deliver on both
            # routes, and the pane id is ground truth this function was
            # handed. Keyed on the typed code, never on message prose (§1.2),
            # and only when an agent name was in play — with no name, target
            # IS the pane id and the fallback would repeat the same call.
            if agent_name and _swallowed_code(exc) == AGENT_NOT_FOUND:
                try:
                    herdr_call("pane", "send-keys", pane_id, "enter", timeout=30.0)
                    enters_delivered += 1
                except Exception as pane_exc:
                    absorb(
                        "recovery-enter-pane",
                        ("pane", "send-keys", pane_id, "enter"),
                        pane_exc,
                    )
        if consumed():
            return
        if round_no + 1 < rounds:
            sleep(0.5)
    summary = _swallowed_summary(failures)
    if submission_recorded is not None:
        # The agent runtime writes the transcript record as the turn starts,
        # which can trail the last Enter by a moment. Give it a bounded grace
        # rather than reaping a pane that has in fact begun work.
        #
        # Only worth spending when a refusal is on the table. The grace is the
        # last stretch of the window this function used to convict at the end
        # of, and where it no longer convicts, waiting it out delays the handoff
        # to the node's own machinery without changing a single answer.
        if refuse_unproven:
            grace_deadline = monotonic() + TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S
            while True:
                if consumed():
                    return
                if monotonic() >= grace_deadline:
                    break
                sleep(0.1)
        summary = _swallowed_summary(failures)
        if enters_delivered == 0:
            raise PromptSubmissionUnobservable(
                "AGENT_PROMPT_UNDELIVERED:{0} after {1} submit attempts{2}".format(
                    target, rounds, summary
                ),
                failures,
            )
        if not proof_observed:
            # Every consultation of the proof predicate raised, so nothing
            # was ever observed about the transcript. Claiming UNSUBMITTED
            # here would assert a fact about the composer that only the dead
            # proof channel could have established. Admission fail-closes;
            # the lane path already pressed Enter and hands off.
            if not refuse_unproven:
                return
            raise PromptSubmissionUnobservable(
                "AGENT_PROMPT_UNOBSERVED:{0} after {1} submit attempts{2}".format(
                    target, rounds, summary
                ),
                failures,
            )
        if not refuse_unproven:
            # Offered, unproven — and on this path that is the whole truthful
            # answer. Enters were delivered and the proof channel answered; it
            # answered "no record yet", which after a bounded look at an
            # unbounded quantity is a statement about the length of the look.
            # The node's liveness and quiescence machinery adjudicates from
            # here (B14), and it is the only reader that can, because it is
            # the only one whose evidence keeps arriving.
            return
        raise PromptNotSubmitted(
            "AGENT_PROMPT_UNSUBMITTED:{0} after {1} submit attempts{2}".format(
                target, rounds, summary
            ),
            failures,
        )
    if enters_delivered == 0:
        # Herdr accepted no Enter at all, so no key ever reached the composer
        # and "it will not submit" was never tested. The recorded failures say
        # which deliveries died and how; the refusal must not launder them
        # into a claim about the composer.
        raise PromptSubmissionUnobservable(
            "AGENT_PROMPT_UNDELIVERED:{0} after {1} submit attempts{2}".format(
                target, rounds, summary
            ),
            failures,
        )
    if baseline is None or not readings:
        # Never a legible before/after pair, so there is no fact about the
        # prompt here at all -- only a fact about herdr. Transient by
        # construction, and the pane is left for the caller to reap exactly as
        # the wedged case is.
        raise PromptSubmissionUnobservable(
            "AGENT_PROMPT_UNOBSERVED:{0} after {1} submit attempts{2}".format(
                target, rounds, summary
            ),
            failures,
        )
    if not refuse_unproven:
        # Same reasoning as the transcript branch above, over a weaker meter:
        # a legible revision that did not move is not proof the composer
        # refused the text, because pasting repaints and a turn need not.
        return
    raise PromptNotSubmitted(
        "AGENT_PROMPT_UNSUBMITTED:{0} after {1} submit attempts{2}".format(
            target, rounds, summary
        ),
        failures,
    )


TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S = 10.0


def prompt_submission_marks(
    handle: LaunchHandle,
    prompt_path: Path,
    *,
    chunk_size: int = 64 * 1024,
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> int:
    """How many times the actor transcript names this exact prompt submission.

    A count rather than a boolean because one actor session is re-prompted
    across a correction cycle, and a repair or re-review legitimately reuses
    the same prompt path.  Presence would then be satisfied by the *previous*
    turn's record the instant a new prompt was offered -- the same stale-proof
    failure the pane revision has, one artifact over.  Callers snapshot the
    count before offering and require it to rise.
    """
    transcript = handle.transcript_path
    if transcript is None:
        return 0
    marker = ("@" + str(Path(prompt_path).resolve())).encode("utf-8")
    if chunk_size < len(marker):
        chunk_size = len(marker)
    total = 0
    overlap = b""
    try:
        with Path(transcript).open("rb") as source:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    return total
                window = overlap + chunk
                total += window.count(marker)
                overlap = window[-(len(marker) - 1) :] if len(marker) > 1 else b""
    except OSError as exc:
        # The count so far is a floor, not the count. Callers comparing counts
        # must know the scan aborted — see `_rising_submission_record`, where
        # a silently partial baseline let a previous turn's record stand in as
        # the current offer's proof.
        if on_error is not None:
            on_error(exc)
        return total


def _rising_submission_record(
    handle: LaunchHandle, prompt_path: Path
) -> Callable[[], bool]:
    """A predicate that turns true when THIS offer is recorded.

    Snapshots the transcript's existing marker count now, so a reused prompt
    path from a previous turn on the same actor cannot stand in as proof.
    Missing transcript cannot prove a turn: the predicate stays false rather
    than handing the caller a live-signal fallback. Paste-repaint moves the
    pane revision whether the composer submitted or not (2026-08-27).

    Only a *clean* scan may serve as the baseline. `prompt_submission_marks`
    returns the count-so-far when its read aborts, and a baseline that
    under-counts an already-marked transcript would turn a previous turn's
    record into this offer's proof — the exact stale-proof failure the
    snapshot exists to prevent, re-entered through a discarded read error. An
    aborted baseline is instead established by the first clean consultation
    (which answers False), and an aborted consultation raises so the caller
    records the dead probe instead of reading it as "not recorded".
    """
    if handle.transcript_path is None:
        return lambda: False

    def clean_marks() -> int:
        aborted: List[BaseException] = []
        count = prompt_submission_marks(handle, prompt_path, on_error=aborted.append)
        if aborted:
            # A transcript that does not exist yet has an EXACT count of
            # zero — nothing was partially read, because nothing was opened.
            # Deferring the baseline here made the first genuine record
            # establish the baseline instead of proving the offer, which
            # spent an extra recovery round (and an extra Enter) on every
            # launch whose transcript had not been created yet.
            if isinstance(aborted[0], FileNotFoundError):
                return 0
            raise aborted[0]
        return count

    baseline: List[Optional[int]] = [None]
    try:
        baseline[0] = clean_marks()
    except OSError:
        # No clean baseline yet; the first clean consultation supplies one.
        pass

    def recorded() -> bool:
        count = clean_marks()
        if baseline[0] is None:
            baseline[0] = count
            return False
        return count > baseline[0]

    return recorded


def wait_for_interactive_agent(
    herdr_call: Callable[..., dict],
    name: str,
    timeout_s: float = 180.0,
) -> None:
    """Block until Herdr reports a reusable coding-agent composer.

    Herdr reports an OMP turn that has rendered its final response as either
    ``idle`` or ``done``. Both leave the composer available for the next
    SHA-bound repair prompt; ``working`` still requires the bounded idle wait.
    """

    def settled(payload: Any) -> bool:
        agent = _agent_record(payload) if isinstance(payload, dict) else None
        if not isinstance(agent, dict):
            return False
        status = agent.get("agent_status") or agent.get("status")
        return status in ("idle", "done")

    # The last probe failure `ready()` absorbed, as `TypeName/typed-code`.
    # A probe raising is a missing observation, not a "no" — but discarding it
    # left the timeout refusal claiming "never became ready" about an agent
    # record that was never read. Diagnostic only; nothing branches on it.
    probe_denial: List[str] = []

    def ready() -> bool:
        try:
            payload = herdr_call("agent", "get", name)
        except RuntimeError as exc:
            del probe_denial[:]
            probe_denial.append(
                "{0}/{1}".format(type(exc).__name__, _swallowed_code(exc) or "no-code")
            )
            return False
        return settled(payload)

    def refusal() -> RuntimeError:
        suffix = " probe={0}".format(probe_denial[0]) if probe_denial else ""
        return RuntimeError(
            "AGENT_INTERACTIVE_READY_TIMEOUT:{0}{1}".format(name, suffix)
        )

    if ready():
        return
    timeout_ms = max(1, int(max(0.001, timeout_s) * 1000))
    try:
        outcome = herdr_call(
            "agent",
            "wait",
            name,
            "--timeout",
            str(timeout_ms),
            timeout=timeout_s + 5.0,
        )
    except RuntimeError as exc:
        if ready():
            return
        raise refusal() from exc
    if settled(outcome) or ready():
        return
    raise refusal()



def wait_for_agent_transcript(
    herdr_call: Callable[..., dict],
    name: str,
    timeout_s: float,
    poll_interval_s: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    launched_cwd: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Poll until Herdr's typed session value resolves to a transcript.

    A path-valued session may arrive after ``agent start``. Claude
    remote-control instead keeps an ID-valued session; after the first prompt
    creates its JSONL, ``_agent_transcript_path`` resolves that exact ID under
    the launched cwd's Claude project directory. ``None`` at the deadline
    leaves absence for the caller to classify.
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
        transcript = _agent_transcript_path(
            _agent_record(payload) if isinstance(payload, dict) else None,
            launched_cwd,
            environment,
        )
        if transcript is not None:
            return transcript
        if time.monotonic() >= deadline:
            return None
        sleep(poll_interval_s)


def _wait_for_available_shell(
    herdr_call: Callable[..., dict],
    pane_id: str,
    timeout_s: float = 30.0,
    settle_polls: int = 5,
) -> None:
    """
    Worth waiting for, and nothing more. This used to *gate* the launch,
    raising `SHELL_NOT_READY` at its deadline, and a wall clock over a
    separate RPC cannot prove what it was asked to prove: the last snapshot is
    already stale when `agent start` reaches the server, so a pane this
    function called ready can be busy a millisecond later and a pane it called
    busy can be free. Herdr runs the same precondition inside `agent start`,
    on the server, with no gap — that check is the authority, it answers with
    a typed `error.code`, and the caller now turns that answer into a typed
    retryable refusal into a fresh pane. Returning at the deadline hands the
    decision to the side that can actually make it.

    That demotion is still right and is deliberately not undone. What it left
    open is what the caller does with the authority's first `no`, and the
    answer was "throw the attempt away": see `_start_agent_when_free` below,
    which re-offers the pane to the server-side check inside a bounded window
    before the refusal is raised. The wait here stays advisory and cheap; the
    decision stays with herdr; only the cost of losing the race changes.
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
            return
        time.sleep(0.1)


#: How long `agent start` keeps re-offering a pane herdr itself calls busy,
#: and how often. Finite by construction: at the deadline the refusal is
#: raised exactly as it was before, so the attempt-level retry still gets its
#: fresh pane and nothing here can wait or loop without end.
AGENT_START_BUSY_WINDOW_S = 10.0
AGENT_START_BUSY_POLL_S = 0.5


def _start_agent_when_free(
    start: Callable[[], dict],
    window_s: float = AGENT_START_BUSY_WINDOW_S,
    poll_s: float = AGENT_START_BUSY_POLL_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Offer the pane to `agent start` until herdr stops calling it busy.

    The gap this closes is a timing accident, not a fault. A freshly split
    pane can look like a lone interactive zsh in the moment before login hooks
    (direnv, keychain lookups) spawn their own foreground processes, and
    `agent start` arriving inside that gap is refused `agent_pane_busy`. On
    run-2a44d226e75a4be391a14f02b78a6d25 that refusal cost node
    `lane-p1-freeze-and-run-log` its fourth attempt at zero turns.

    Retrying the *server-side* check rather than re-gating on the client's is
    deliberate, and `_wait_for_available_shell` above records why the client
    gate was demoted: a wall clock over a separate RPC cannot prove what it is
    asked to prove, because the last snapshot is already stale when `agent
    start` reaches the server. That reasoning still holds, so the advisory
    wait stays advisory and the decision stays where it can actually be made —
    herdr's own precondition, which answers with a typed `error.code`. This
    just stops throwing away an attempt over the first `no` when herdr will
    say `yes` a second later.

    Only the codes herdr's own vocabulary marks survivable are re-offered
    (`TRANSIENT_HERDR_ERROR_CODES`, read off the typed field and never matched
    out of the message, §1.2). Everything else raises on the first refusal,
    unchanged. Two bounds, and both are needed: the window here spends
    seconds, and the refusal it eventually raises is retryable, so the
    attempt-level budget spends launches — into a fresh pane, which is the
    only remedy left if this pane is durably occupied.
    """
    deadline = monotonic() + max(0.0, window_s)
    while True:
        try:
            return start()
        except HerdrCallError as exc:
            if exc.code not in TRANSIENT_HERDR_ERROR_CODES or monotonic() >= deadline:
                raise
            sleep(poll_s)
            # The poll is spent *inside* the window, not on top of it. Checking
            # the deadline only before the sleep let the sleep carry the clock
            # past it and buy one more `agent start` at `deadline + poll_s`, so
            # the bound described an exit condition rather than the offers it
            # authorised. Re-checking here makes every offer fall inside the
            # window; the refusal raised is the same typed, retryable one.
            if monotonic() >= deadline:
                raise


class _TabLayout:
    """One lane child's pane grid: parent/child workspace ids, tab, slots.

    Parent and child workspace ids are distinct fields. The parent is the run
    Space group; the child is the linked worktree workspace whose tab holds
    these panes. Splits stay inside that child.
    """

    __slots__ = (
        "tab_id",
        "panes",
        "claimed",
        "cols",
        "lock",
        "role_panes",
        "refresh_roles",
        "parent_workspace_id",
        "child_workspace_id",
        "lane_key",
        "lane_label",
    )

    def __init__(
        self,
        tab_id: str,
        panes: List[Optional[str]],
        claimed: int,
        *,
        parent_workspace_id: str = "",
        child_workspace_id: str = "",
        lane_key: str = "",
        lane_label: str = "",
    ) -> None:
        self.tab_id = tab_id
        self.panes: List[Optional[str]] = list(panes)
        self.claimed = claimed
        self.cols = 1
        self.lock = threading.Lock()
        self.role_panes: Dict[str, str] = {}
        self.refresh_roles: set[str] = set()
        self.parent_workspace_id = str(parent_workspace_id or "")
        self.child_workspace_id = str(child_workspace_id or "")
        self.lane_key = str(lane_key or "")
        self.lane_label = str(lane_label or "")

    def nearest_live(self, index: int) -> Optional[str]:
        last = len(self.panes) - 1
        for slot in range(min(index, last), -1, -1):
            pane = self.panes[slot]
            if pane:
                return pane
        for slot in range(min(index, last) + 1, last + 1):
            pane = self.panes[slot]
            if pane:
                return pane
        return None

    def forget(self, pane_id: str) -> bool:
        for slot, pane in enumerate(self.panes):
            if pane == pane_id:
                self.panes[slot] = None
                for role, held in list(self.role_panes.items()):
                    if held == pane_id:
                        del self.role_panes[role]
                return True
        return False

    def empty(self) -> bool:
        return not any(self.panes)



class HerdrLauncher:
    def __init__(
        self,
        *,
        herdr_path: Path,
        omp_path: Path,
        claude_path: Path,
        admitted_routes: AdmittedRouteSet,
        provision_argv: Sequence[str] = (),
        workspace_label: str = "",
    ) -> None:
        if not isinstance(admitted_routes, AdmittedRouteSet):
            raise TypeError("VERIFIED_ADMITTED_ROUTES_REQUIRED")
        self.herdr_path = Path(herdr_path)
        self.omp_path = Path(omp_path)
        self.claude_path = Path(claude_path)
        self.admitted_routes = admitted_routes
        self.provision_argv = tuple(provision_argv)
        #: Label for a parent Space Maestro creates when none is open on the
        #: repository (the operator's own Space, when open, is the parent and
        #: keeps its label). Empty keeps the non-run path: panes split from
        #: the caller's once-resolved pane, with no workspaces.
        self.workspace_label = str(workspace_label or "")
        self.agent_start_busy_window_s = AGENT_START_BUSY_WINDOW_S
        self.quiescence_confirm_s = AGENT_QUIESCENCE_CONFIRM_S
        self._handles_lock = threading.RLock()
        self._handles: Dict[str, LaunchHandle] = {}
        self._tailers: Dict[str, TranscriptTailer] = {}
        self._quiescent_since: Dict[str, Tuple[float, int]] = {}
        self._proven_absent: Dict[str, LaunchHandle] = {}
        self._split_parent_id: Optional[str] = None
        #: Parent run workspace id. Not the lane child.
        self._parent_workspace_id: str = ""
        #: Deprecated alias of the parent id for the non-run split-parent path.
        self._workspace_id: str = ""
        self._run_id: str = ""
        self._repository_fingerprint: str = ""
        self._repository_root: Path = Path()
        #: lane_key -> linked child layout.
        self._tabs: Dict[str, _TabLayout] = {}
        self._seed_tab_id: str = ""
        self._role_handles: Dict[Tuple[str, str], LaunchHandle] = {}
        self._cleaned_absent: set[str] = set()


    @property
    def workspace_id(self) -> str:
        """The parent run workspace bound to this launcher, if any."""
        with self._handles_lock:
            return self._parent_workspace_id or self._workspace_id



    def _herdr(
        self, *args: str, env: Optional[Mapping[str, str]] = None, timeout: float = 30.0
    ) -> dict:
        merged = dict(os.environ)
        if env:
            merged.update(env)
        try:
            result = subprocess.run(
                [str(self.herdr_path), *args],
                capture_output=True,
                text=True,
                env=merged,
                timeout=timeout,
                check=False,
            )
        except (OSError, ValueError) as exc:
            raise LaunchRefused(
                LaunchRefusal.HERDR_UNAVAILABLE,
                "{}: {}: {}".format(self.herdr_path, type(exc).__name__, exc),
            ) from exc
        if result.returncode != 0:
            refusal = (result.stderr or result.stdout).strip()
            raise HerdrCallError(
                "LAUNCH_REFUSED:{}".format(refusal[-400:]), herdr_error_code(refusal)
            )
        try:
            # A herdr verb that succeeds with nothing to say prints nothing at
            # all: `pane send-text`, `pane send-keys`, and `agent send-keys`
            # all exit 0 with empty stdout. Decoding that as JSON raises, so
            # before this the two-call submission path -- the fix for the
            # composer eating an atomic Enter -- refused every prompt with
            # PROTOCOL_INVALID_JSON, retried as LAUNCHER_TRANSIENT, and burned
            # the launcher budget without a single agent ever starting a turn.
            # An exit-0 with no payload is a success carrying no payload.
            payload = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            if _is_text_read(args):
                return {"result": {"text": result.stdout or ""}}
            raise RuntimeError("PROTOCOL_INVALID_JSON") from exc
        if not isinstance(payload, dict):
            if _is_text_read(args):
                return {"result": {"text": result.stdout or ""}}
            raise RuntimeError("PROTOCOL_INVALID_RESPONSE")
        return payload

    def _split_parent(self, environment: Mapping[str, str]) -> str:
        """The pane id every split of this run is taken from, resolved once.

        `--current` is not an identifier, it is a server-side selector over
        mutable state: whichever pane holds focus at the instant the split
        arrives. Two lanes splitting concurrently read that selector at two
        different instants, so the second can split a pane the first has just
        created — and did, on 2026-08-18, when one lane's agent landed in a
        pane the sibling launch had opened 30ms earlier. Resolving the
        selector once, to the pane id it named the first time it was asked,
        makes every subsequent split name a fixed pane and removes the shared
        mutable read from the race entirely.

        Cached deliberately: re-asking would reintroduce the moving target.

        **An unanswerable selector is a refusal, not a fallback.** Using
        `--current` unchanged used to be the answer here, and it was wrong
        twice over: it reinstated exactly the race the caching removes, and
        because focus can sit in any workspace, each such split landed
        wherever focus happened to be — the run whose agents scattered across
        w13F, w13G, w13H, w13J and w13K while its first pane was in w13A. A
        lifecycle decision keyed on ambient mutable state is what §1.2
        forbids, so this raises `SPLIT_PARENT_UNRESOLVED` and the failure is
        not cached: the member is non-deterministic, the launcher budget is
        spent, and the next attempt genuinely re-asks herdr.

        Resolved on first use rather than in `__init__` because a constructor
        that calls herdr cannot be built offline at all, and a launcher that
        cannot exist while herdr is briefly unreachable is a worse failure
        than a refused launch. It is still resolved once per launcher.
        """
        with self._handles_lock:
            if self._split_parent_id is None:
                try:
                    payload = self._herdr("pane", "current", env=environment)
                except BaseException as exc:
                    raise LaunchRefused(
                        LaunchRefusal.SPLIT_PARENT_UNRESOLVED,
                        "{0}: {1}".format(type(exc).__name__, exc),
                    ) from exc
                pane = _extract(payload, "pane")
                pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
                if not pane_id:
                    raise LaunchRefused(
                        LaunchRefusal.SPLIT_PARENT_UNRESOLVED, "NO_PANE_ID"
                    )
                self._split_parent_id = str(pane_id)
                self._workspace_id = workspace_of(self._split_parent_id)
            return self._split_parent_id

    def _bind_run_identity(self, spec: LaunchSpec) -> None:
        run_id = str(spec.run_id or "")
        fingerprint = str(spec.repository_fingerprint or "")
        requested = str(spec.workspace_label or "")
        repo_root = Path(spec.repository_root) if spec.repository_root else Path()
        with self._handles_lock:
            if run_id:
                if self._run_id and self._run_id != run_id:
                    raise LaunchRefused(
                        LaunchRefusal.WORKSPACE_DRIFT,
                        "run {} != {}".format(run_id, self._run_id),
                        pane_created=False,
                    )
                self._run_id = run_id
            if fingerprint:
                if (
                    self._repository_fingerprint
                    and self._repository_fingerprint != fingerprint
                ):
                    raise LaunchRefused(
                        LaunchRefusal.WORKSPACE_DRIFT,
                        "repo {} != {}".format(
                            fingerprint, self._repository_fingerprint
                        ),
                        pane_created=False,
                    )
                self._repository_fingerprint = fingerprint
            if repo_root != Path():
                resolved = git_primary_workdir(repo_root)
                if self._repository_root != Path() and self._repository_root != resolved:
                    raise LaunchRefused(
                        LaunchRefusal.WORKSPACE_DRIFT,
                        "repo cwd {} != {}".format(resolved, self._repository_root),
                        pane_created=False,
                    )
                self._repository_root = resolved
            if requested and self.workspace_label and requested != self.workspace_label:
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_DRIFT,
                    "workspace label {} != {}".format(
                        requested, self.workspace_label
                    ),
                    pane_created=False,
                )
            if requested and not self.workspace_label:
                self.workspace_label = requested

    def _parent_identity_tokens(self) -> Dict[str, str]:
        tokens = {METADATA_TOKEN_KIND: METADATA_KIND_RUN}
        if self._run_id:
            tokens[METADATA_TOKEN_RUN] = self._run_id
        if self._repository_fingerprint:
            tokens[METADATA_TOKEN_REPO] = self._repository_fingerprint
        return tokens

    def _lane_identity_tokens(self, lane_id: str, parent_id: str) -> Dict[str, str]:
        tokens = {
            METADATA_TOKEN_KIND: METADATA_KIND_LANE,
            METADATA_TOKEN_LANE: str(lane_id or ""),
            METADATA_TOKEN_PARENT: str(parent_id or ""),
        }
        if self._run_id:
            tokens[METADATA_TOKEN_RUN] = self._run_id
        if self._repository_fingerprint:
            tokens[METADATA_TOKEN_REPO] = self._repository_fingerprint
        return tokens

    def _pane_identity_tokens(
        self, lane_id: str, role: str, parent_id: str
    ) -> Dict[str, str]:
        tokens = self._lane_identity_tokens(lane_id, parent_id)
        if role:
            tokens[METADATA_TOKEN_ROLE] = str(role)
        tokens[METADATA_TOKEN_SCRATCH] = METADATA_SCRATCH_REDIRECT
        return tokens

    def _identity_complete(self, tokens: Mapping[str, str]) -> bool:
        if not self._run_id or not self._repository_fingerprint:
            return False
        required = (METADATA_TOKEN_KIND, METADATA_TOKEN_RUN, METADATA_TOKEN_REPO)
        return all(tokens.get(key) for key in required)

    def _tag_workspace(
        self,
        workspace_id: str,
        tokens: Mapping[str, str],
        environment: Mapping[str, str],
    ) -> None:
        flags = _metadata_token_flags(tokens)
        if not workspace_id or not flags:
            return
        self._herdr(
            "workspace",
            "report-metadata",
            workspace_id,
            "--source",
            MAESTRO_METADATA_SOURCE,
            *flags,
            env=environment,
        )

    def _tag_pane(
        self,
        pane_id: str,
        tokens: Mapping[str, str],
        environment: Mapping[str, str],
    ) -> None:
        flags = _metadata_token_flags(tokens)
        if not pane_id or not flags:
            return
        self._herdr(
            "pane",
            "report-metadata",
            pane_id,
            "--source",
            MAESTRO_METADATA_SOURCE,
            *flags,
            env=environment,
        )

    def _workspace_record(
        self, workspace_id: str, environment: Mapping[str, str]
    ) -> dict:
        try:
            payload = self._herdr("workspace", "get", workspace_id, env=environment)
        except HerdrCallError as exc:
            if exc.code == WORKSPACE_NOT_FOUND:
                raise _WorkspaceGone(workspace_id) from exc
            raise
        workspace = _extract(payload, "workspace")
        if not isinstance(workspace, dict) or not workspace.get("workspace_id"):
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_UNRESOLVED,
                "NO_WORKSPACE_ID",
                pane_created=False,
            )
        return workspace

    def _worktree_is_linked(self, workspace: Mapping[str, object]) -> bool:
        info = workspace.get("worktree")
        return isinstance(info, dict) and info.get("is_linked_worktree") is True

    def _parent_binding_defect(
        self, workspace: object, repo_cwd: Path
    ) -> str:
        """Why a created parent is not the repository's source Space, or ``""``.

        Real `WorkspaceInfo.worktree` is `WorkspaceWorktreeInfo | null`
        (`repo_key, repo_name, repo_root, checkout_path, is_linked_worktree`).
        The parent must be a non-linked workspace whose `repo_root` is the
        primary worktree passed as `--cwd`.
        """
        if not isinstance(workspace, dict):
            return "NO_WORKSPACE_RECORD"
        info = workspace.get("worktree")
        if not isinstance(info, dict):
            return "NO_WORKTREE_BINDING"
        if info.get("is_linked_worktree") is not False:
            return "PARENT_IS_LINKED"
        repo_root = str(info.get("repo_root") or "")
        if not repo_root:
            return "NO_REPO_ROOT"
        if Path(repo_root).resolve() != Path(repo_cwd).resolve():
            return "REPO_ROOT_MISMATCH:{}".format(repo_root)
        return ""

    def _run_workspace(self, environment: Mapping[str, str]) -> str:
        """The parent Space lanes hang under: the operator's Space open on
        the repository when there is one, else one Maestro creates once."""
        with self._handles_lock:
            if self._parent_workspace_id:
                return self._parent_workspace_id
            if not self.workspace_label:
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_UNRESOLVED,
                    "NO_WORKSPACE_LABEL",
                    pane_created=False,
                )
            adopted = self._adopt_existing_workspace(environment)
            if adopted:
                self._parent_workspace_id = adopted
                self._workspace_id = adopted
                return adopted
            repo_cwd = self._repository_root
            if repo_cwd == Path():
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_UNRESOLVED,
                    "REPO_PARENT_CWD_REQUIRED",
                    pane_created=False,
                )
            # Identity is proven writable before the object exists: a parent
            # that cannot be tagged must not be created.
            parent_tokens = self._parent_identity_tokens()
            _metadata_token_flags(parent_tokens)
            try:
                payload = self._herdr(
                    "workspace",
                    "create",
                    "--cwd",
                    str(repo_cwd),
                    "--label",
                    self.workspace_label,
                    "--no-focus",
                    env=environment,
                )
            except BaseException as exc:
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_UNRESOLVED,
                    "{0}: {1}".format(type(exc).__name__, exc),
                ) from exc
            workspace = _extract(payload, "workspace")
            workspace_id = (
                workspace.get("workspace_id") if isinstance(workspace, dict) else None
            )
            if not workspace_id:
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_UNRESOLVED, "NO_WORKSPACE_ID"
                )
            parent_id = str(workspace_id)
            # Herdr groups a linked child under the workspace whose checkout
            # is the repository's source. A parent Herdr did not bind to the
            # primary worktree would collect its lanes under some other Space
            # (the `run -> run -> lane` sidebar), so the binding is proven on
            # the created record and an unbound parent is closed, not kept.
            unbound = self._parent_binding_defect(workspace, repo_cwd)
            if unbound:
                try:
                    self._close_workspace_absent_ok(parent_id, environment)
                except HerdrCallError as exc:
                    unbound = "{}; close refused: {}".format(unbound, exc)
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_UNRESOLVED,
                    "RUN_WORKSPACE_UNBOUND:{}:{}".format(parent_id, unbound),
                    pane_created=False,
                )
            self._tag_workspace(parent_id, parent_tokens, environment)
            self._parent_workspace_id = parent_id
            self._workspace_id = parent_id
            seed = _extract(payload, "tab")
            if isinstance(seed, dict) and seed.get("tab_id"):
                self._seed_tab_id = str(seed["tab_id"])
            return parent_id

    def _close_seed_tab(self, environment: Mapping[str, str]) -> None:
        """Drop the shell tab `workspace create` opens, once a child exists."""
        tab_id, self._seed_tab_id = self._seed_tab_id, ""
        if not tab_id:
            return
        try:
            self._herdr("tab", "close", tab_id, env=environment)
        except BaseException:
            return

    def _adopt_existing_workspace(self, environment: Mapping[str, str]) -> str:
        """Adopt the unique non-linked Space bound to the primary checkout.

        The parent is the operator's own Space when one is open on the
        repository (the one the command is run from); lanes are its linked
        children. Identity is the repository binding -- `worktree.repo_root`
        resolves to the primary worktree and the Space is not linked --
        never run tokens: the operator's Space is not Maestro's to tag, and
        several runs may share it. Two such Spaces are refused by name; none
        means Maestro creates its own (`_run_workspace`).
        """
        root = self._repository_root
        if root == Path():
            return ""
        try:
            payload = self._herdr("workspace", "list", env=environment)
        except BaseException as exc:
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_UNRESOLVED,
                "workspace list: {0}: {1}".format(type(exc).__name__, exc),
                pane_created=False,
            ) from exc
        bound: List[str] = []
        described: List[str] = []
        for item in _herdr_list(payload, "workspaces"):
            workspace_id = str(item.get("workspace_id") or "")
            if not workspace_id or self._parent_binding_defect(item, root):
                continue
            bound.append(workspace_id)
            tagged = _herdr_tokens(item).get(METADATA_TOKEN_KIND) == METADATA_KIND_RUN
            described.append(
                "{}({},{})".format(
                    workspace_id,
                    _herdr_label(item),
                    "maestro-tagged" if tagged else "untagged",
                )
            )
        if len(bound) > 1:
            # Named with label and tagging so the operator can tell the
            # Space Maestro created from their own and close the right one.
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "DUPLICATE_RUN_WORKSPACE:{}".format(",".join(described)),
                pane_created=False,
            )
        if not bound:
            return ""
        workspace_id = bound[0]
        try:
            live = self._workspace_record(workspace_id, environment)
        except _WorkspaceGone:
            # Listed, then closed before it could be read: absent.
            return ""
        defect = self._parent_binding_defect(live, root)
        if defect:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "RUN_WORKSPACE_BINDING_MISMATCH:{}:{}".format(workspace_id, defect),
                pane_created=False,
            )
        return workspace_id

    def _validated_role_layout(
        self,
        tab_id: str,
        panes_payload: Mapping[str, object],
        *,
        parent_workspace_id: str = "",
        child_workspace_id: str = "",
        lane_key: str = "",
        lane_label: str = "",
    ) -> _TabLayout:
        pane_ids: List[Optional[str]] = []
        role_panes: Dict[str, str] = {}
        refresh_roles: set[str] = set()
        unlabeled: List[str] = []
        for pane in _herdr_list(panes_payload, "panes"):
            pane_tab = str(pane.get("tab_id") or "")
            if pane_tab and pane_tab != tab_id:
                continue
            pane_id = str(pane.get("pane_id") or "")
            if not pane_id:
                raise LaunchRefused(
                    LaunchRefusal.BINDING_MISMATCH,
                    "ROLE_PANE_ID_MISSING:{}".format(tab_id),
                    pane_created=False,
                )
            pane_ws = (
                str(pane.get("workspace_id") or "")
                or workspace_of(pane_tab)
                or workspace_of(pane_id)
            )
            if child_workspace_id and pane_ws and pane_ws != child_workspace_id:
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_DRIFT,
                    "{}!={}".format(pane_ws, child_workspace_id),
                    pane_created=False,
                )
            pane_label = _herdr_label(pane)
            role = pane_role_for_label(pane_label) if pane_label else ""
            if pane_label and not role:
                raise LaunchRefused(
                    LaunchRefusal.BINDING_MISMATCH,
                    "UNMIGRATED_PANE_LABEL:{}".format(pane_label),
                    pane_created=False,
                )
            if role:
                if role in role_panes:
                    raise LaunchRefused(
                        LaunchRefusal.BINDING_MISMATCH,
                        "DUPLICATE_ROLE_PANE:{}".format(role),
                        pane_created=False,
                    )
                role_panes[role] = pane_id
                tokens = _herdr_tokens(pane)
                if (
                    pane.get("agent_status") == "unknown"
                    or tokens.get(METADATA_TOKEN_SCRATCH)
                    != METADATA_SCRATCH_REDIRECT
                ):
                    refresh_roles.add(role)
            else:
                unlabeled.append(pane_id)
            pane_ids.append(pane_id)
        if unlabeled and role_panes and len(unlabeled) > 1:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "UNLABELLED_LANE_PANES",
                pane_created=False,
            )
        layout = _TabLayout(
            tab_id=tab_id,
            panes=pane_ids or [None],
            claimed=len(role_panes),
            parent_workspace_id=parent_workspace_id,
            child_workspace_id=child_workspace_id,
            lane_key=lane_key,
            lane_label=lane_label,
        )
        layout.role_panes.update(role_panes)
        layout.refresh_roles.update(refresh_roles)
        if not pane_ids:
            layout.claimed = 0
        return layout

    def _layout_from_child_workspace(
        self,
        child_workspace_id: str,
        parent_workspace_id: str,
        group: str,
        label: str,
        environment: Mapping[str, str],
    ) -> _TabLayout:
        try:
            payload = self._herdr(
                "tab", "list", "--workspace", child_workspace_id, env=environment
            )
        except HerdrCallError as exc:
            if exc.code == WORKSPACE_NOT_FOUND:
                raise _WorkspaceGone(child_workspace_id) from exc
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "{0}: {1}".format(type(exc).__name__, exc),
            ) from exc
        tab_ids = [
            str(tab.get("tab_id") or "")
            for tab in _herdr_list(payload, "tabs")
            if tab.get("tab_id")
        ]
        if not tab_ids:
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "NO_CHILD_TAB:{}".format(child_workspace_id),
                pane_created=False,
            )
        try:
            panes_payload = self._herdr(
                "pane", "list", "--workspace", child_workspace_id, env=environment
            )
        except HerdrCallError as exc:
            if exc.code == WORKSPACE_NOT_FOUND:
                raise _WorkspaceGone(child_workspace_id) from exc
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "{0}: {1}".format(type(exc).__name__, exc),
                pane_created=False,
            ) from exc
        # The role panes name the tab, not the listing order: a child that
        # holds more than one tab (an operator opened one) used to be read
        # through `tabs[0]`, which hid every role pane in another tab and
        # split duplicates beside them.
        role_tabs: List[str] = []
        for pane in _herdr_list(panes_payload, "panes"):
            pane_label = _herdr_label(pane)
            if pane_label and pane_role_for_label(pane_label):
                pane_tab = str(pane.get("tab_id") or "")
                if pane_tab and pane_tab not in role_tabs:
                    role_tabs.append(pane_tab)
        if len(role_tabs) > 1:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "ROLE_PANES_SPAN_TABS:{}".format(label),
                pane_created=False,
            )
        if role_tabs:
            tab_id = role_tabs[0]
        elif len(tab_ids) == 1:
            tab_id = tab_ids[0]
        else:
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "AMBIGUOUS_LANE_TAB:{}".format(label),
                pane_created=False,
            )
        return self._validated_role_layout(
            tab_id,
            panes_payload,
            parent_workspace_id=parent_workspace_id,
            child_workspace_id=child_workspace_id,
            lane_key=group,
            lane_label=label,
        )

    def _lane_pinned_to_dead_parent(
        self,
        child_id: str,
        tokens: Mapping[str, str],
        expected: Mapping[str, str],
        environment: Mapping[str, str],
    ) -> bool:
        """Whether a tagged lane child is ours but names a parent that is gone.

        The `parent` token pins a child to the Space id it was opened under.
        When the operator closes and reopens their Space (a new id) and Herdr
        leaves the linked children open, the child still matches this run,
        repository and lane but its `parent` names a workspace `workspace
        get` no longer knows. Such a child is retagged to the current parent
        and adopted; a child whose tagged parent is a different live Space
        stays a refusal. Keyed on typed fields only: token equality and the
        typed `workspace_not_found` answer, never labels.
        """
        without_parent = {
            key: value
            for key, value in expected.items()
            if key != METADATA_TOKEN_PARENT
        }
        if not without_parent or not _tokens_match(tokens, without_parent):
            return False
        tagged_parent = str(tokens.get(METADATA_TOKEN_PARENT) or "")
        current_parent = str(expected.get(METADATA_TOKEN_PARENT) or "")
        if not tagged_parent or tagged_parent == current_parent:
            return False
        try:
            self._workspace_record(tagged_parent, environment)
        except _WorkspaceGone:
            return True
        return False

    def _repin_lane(
        self, child_id: str, parent_id: str, environment: Mapping[str, str]
    ) -> None:
        """Point a surviving lane child, and every Maestro pane in it, at the
        parent resolved now. Only the `parent` token is rewritten; the panes'
        other tokens (role, scratch contract) are theirs to keep."""
        repin = {METADATA_TOKEN_PARENT: parent_id}
        self._tag_workspace(child_id, repin, environment)
        try:
            panes = _herdr_list(
                self._herdr("pane", "list", "--workspace", child_id, env=environment),
                "panes",
            )
        except HerdrCallError as exc:
            if exc.code == WORKSPACE_NOT_FOUND:
                raise _WorkspaceGone(child_id) from exc
            raise
        for pane in panes:
            pane_id = str(pane.get("pane_id") or "")
            tagged = _herdr_tokens(pane).get(METADATA_TOKEN_PARENT, "")
            if pane_id and tagged and tagged != parent_id:
                self._tag_pane(pane_id, repin, environment)

    def _adopt_existing_lane(
        self,
        parent_id: str,
        group: str,
        label: str,
        worktree: Path,
        environment: Mapping[str, str],
    ) -> Optional[_TabLayout]:
        """Adopt the unique linked child whose metadata identity matches.

        A child with no tokens is adopted only when it is provably ours by
        the other identity Herdr holds: it is listed under this parent
        (`source.source_workspace_id == parent`) and its `WorktreeInfo.path`
        is this role's checkout. That is the window "open succeeded, process
        stopped before tagging"; the child is tagged here and adopted, so a
        restart converges instead of refusing the lane forever. A token-less
        child at any other path stays a label-only refusal.
        """
        expected = self._lane_identity_tokens(label or group, parent_id)
        if not self._identity_complete(expected) or not expected.get(
            METADATA_TOKEN_LANE
        ):
            return None
        try:
            payload = self._herdr(
                "worktree", "list", "--workspace", parent_id, env=environment
            )
        except HerdrCallError as exc:
            if exc.code == WORKSPACE_NOT_FOUND:
                raise _WorkspaceGone(parent_id) from exc
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "{0}: {1}".format(type(exc).__name__, exc),
                pane_created=False,
            ) from exc
        source = _extract(payload, "source")
        if isinstance(source, dict):
            source_id = str(source.get("source_workspace_id") or "")
            if source_id and source_id != parent_id:
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_DRIFT,
                    "{}!={}".format(source_id, parent_id),
                    pane_created=False,
                )
        matches: List[str] = []
        untagged_ours: List[str] = []
        pinned_elsewhere: List[str] = []
        label_only = 0
        for item in _herdr_list(payload, "worktrees"):
            child_id = str(item.get("open_workspace_id") or "")
            if not child_id:
                continue
            if item.get("is_linked_worktree") is not True:
                continue
            try:
                live = self._workspace_record(child_id, environment)
            except _WorkspaceGone:
                continue
            tokens = _herdr_tokens(live)
            if not tokens:
                if _same_resolved_path(item.get("path"), worktree):
                    untagged_ours.append(child_id)
                elif _herdr_label(live) == label or _herdr_label(item) == label:
                    label_only += 1
                continue
            if _tokens_match(tokens, expected):
                matches.append(child_id)
            elif self._lane_pinned_to_dead_parent(
                child_id, tokens, expected, environment
            ):
                # Listed under the current parent, ours by run/repo/lane,
                # pinned to a Space that no longer exists: re-pin it here.
                self._repin_lane(child_id, parent_id, environment)
                matches.append(child_id)
            elif _tokens_match(
                tokens,
                {k: v for k, v in expected.items() if k != METADATA_TOKEN_PARENT},
            ):
                pinned_elsewhere.append(child_id)
        if pinned_elsewhere and not matches and not untagged_ours:
            # Ours by run/repo/lane but pinned to a different live Space:
            # refused here rather than after a `worktree open` that would
            # only report it already open.
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "LANE_WORKSPACE_TOKEN_MISMATCH:{}".format(pinned_elsewhere[0]),
                pane_created=False,
            )
        if label_only and not matches and not untagged_ours:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "LABEL_ONLY_LANE_WORKSPACE:{}".format(label),
                pane_created=False,
            )
        if len(matches) + len(untagged_ours) > 1:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "DUPLICATE_LANE_WORKSPACE:{}".format(label),
                pane_created=False,
            )
        if untagged_ours:
            child_id = untagged_ours[0]
            self._tag_workspace(child_id, expected, environment)
            return self._layout_from_child_workspace(
                child_id, parent_id, group, label, environment
            )
        if not matches:
            return None
        return self._layout_from_child_workspace(
            matches[0], parent_id, group, label, environment
        )


    def _role_key(self, spec: LaunchSpec) -> Optional[Tuple[str, str]]:
        role = str(spec.pane_role or "")
        if not role:
            return None
        return (str(spec.lane_key or ""), role)

    def _register_role_handle(self, spec: LaunchSpec, handle: LaunchHandle) -> None:
        key = self._role_key(spec)
        if key is None:
            return
        with self._handles_lock:
            self._role_handles[key] = handle

    def _existing_role_handle(self, spec: LaunchSpec) -> Optional[LaunchHandle]:
        key = self._role_key(spec)
        if key is None:
            return None
        with self._handles_lock:
            return self._role_handles.get(key)

    def _discard_role_handle(self, spec: LaunchSpec, handle: LaunchHandle) -> None:
        key = self._role_key(spec)
        with self._handles_lock:
            if key is not None and self._role_handles.get(key) is handle:
                self._role_handles.pop(key, None)
            token = handle.correlation_token
            if self._handles.get(token) is handle:
                self._handles.pop(token, None)
            self._tailers.pop(token, None)

    def _validated_existing_role_handle(
        self, spec: LaunchSpec, environment: Mapping[str, str]
    ) -> Optional[LaunchHandle]:
        handle = self._existing_role_handle(spec)
        if handle is None:
            return None
        agent = self._fetch_agent(handle.agent_name, environment)
        if agent is None:
            self._discard_role_handle(spec, handle)
            return None
        status = _agent_status_of(agent)
        if status not in REUSABLE_AGENT_STATUSES:
            self._refuse_live_pane(
                "AGENT_STATUS_UNOBSERVABLE:{}".format(status or "<missing>")
            )
        pane = self._prove_live_pane(spec, agent, environment)
        if (
            self.workspace_label
            and _herdr_tokens(pane).get(METADATA_TOKEN_SCRATCH)
            != METADATA_SCRATCH_REDIRECT
        ):
            self._discard_role_handle(spec, handle)
            return None
        pane_id = str(pane.get("pane_id") or "")
        actual = Path(str(pane.get("cwd"))).resolve()
        expected = spec.worktree.resolve()
        if pane_id != handle.pane_id:
            self._refuse_live_pane("{}!={}".format(pane_id, handle.pane_id))
        if actual != expected or actual != handle.launched_cwd.resolve():
            self._refuse_live_pane(
                "{}!={}".format(actual, expected),
            )
        tab_id = str(pane.get("tab_id") or "")
        workspace_id = (
            str(pane.get("workspace_id") or "")
            or workspace_of(tab_id)
            or workspace_of(pane_id)
        )
        if handle.tab_id and tab_id != handle.tab_id:
            self._refuse_live_pane("{}!={}".format(tab_id, handle.tab_id))
        if handle.workspace_id and workspace_id != handle.workspace_id:
            self._refuse_live_pane("{}!={}".format(workspace_id, handle.workspace_id))
        return handle

    def _stable_agent_in_pane(
        self, name: str, pane_id: str, environment: Mapping[str, str]
    ) -> bool:
        """Whether Herdr now holds the stable agent `name` in `pane_id`."""
        try:
            agent = self._fetch_agent(name, environment)
        except (HerdrCallError, RuntimeError):
            return False
        return agent is not None and str(agent.get("pane_id") or "") == pane_id

    def _fetch_agent(self, name: str, environment: Mapping[str, str]) -> Optional[dict]:
        try:
            payload = self._herdr("agent", "get", name, env=environment)
        except HerdrCallError as exc:
            if exc.code == AGENT_NOT_FOUND:
                return None
            raise
        agent = _agent_record(payload)
        if not isinstance(agent, dict):
            raise RuntimeError("HERDR_AGENT_RECORD_INVALID:{0}".format(name))
        if not _agent_named(agent, name):
            raise RuntimeError("HERDR_AGENT_NAME_MISMATCH:{0}".format(name))
        return agent

    def _role_stage_worktree_scope(self, spec: LaunchSpec) -> Path:
        """Run/lane/role directory that owns this role's checkout.

        Persistent tester/builder panes survive REVISE and digest change.
        Scope is derived from the current spec path, never a stage token.
        """
        worktree = spec.worktree.resolve()
        run_id = str(spec.run_id or "")
        lane = str(spec.lane_key or "")
        role = str(spec.pane_role or "")
        if not (run_id and lane and role):
            return worktree
        needle = (run_id, lane, role)
        parts = worktree.parts
        span = len(needle)
        for index in range(0, len(parts) - span + 1):
            if parts[index : index + span] == needle:
                return Path(*parts[: index + span])
        return worktree

    def _pane_cwd_within(
        self, pane_id: str, scope: Path, environment: Mapping[str, str]
    ) -> bool:
        """Whether Herdr reports `pane_id` with a cwd inside `scope`."""
        try:
            payload = self._herdr("pane", "get", pane_id, env=environment)
        except HerdrCallError:
            return False
        pane = _extract(payload, "pane")
        cwd = pane.get("cwd") if isinstance(pane, dict) else None
        if not cwd:
            return False
        try:
            Path(str(cwd)).resolve().relative_to(Path(scope).resolve())
        except ValueError:
            return False
        return True

    def _refuse_live_pane(
        self,
        detail: str,
        exc: Optional[BaseException] = None,
    ) -> NoReturn:
        if exc is None:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH, detail, pane_created=False
            )
        raise LaunchRefused(
            LaunchRefusal.BINDING_MISMATCH,
            "{0}: {1}: {2}".format(detail, type(exc).__name__, exc),
            pane_created=False,
        ) from exc

    def _prove_live_pane(
        self,
        spec: LaunchSpec,
        agent: Mapping[str, object],
        environment: Mapping[str, str],
    ) -> dict:
        pane_id = str(agent.get("pane_id") or "")
        if not pane_id:
            self._refuse_live_pane("NO_PANE_ID")
        try:
            pane_payload = self._herdr("pane", "get", pane_id, env=environment)
        except BaseException as exc:
            self._refuse_live_pane("pane get", exc)
        pane = _extract(pane_payload, "pane")
        if not isinstance(pane, dict) or str(pane.get("pane_id") or "") != pane_id:
            self._refuse_live_pane(pane_id)
        cwd = pane.get("cwd")
        if not cwd:
            self._refuse_live_pane("NO_CWD")
        actual = Path(str(cwd)).resolve()
        scope = self._role_stage_worktree_scope(spec)
        try:
            actual.relative_to(scope)
        except ValueError:
            self._refuse_live_pane("{}!={}".format(actual, scope))
        self._prove_live_placement(spec, pane, environment)
        return pane

    def _prove_live_placement(
        self,
        spec: LaunchSpec,
        pane: Mapping[str, object],
        environment: Mapping[str, str],
    ) -> None:
        role = str(spec.pane_role or "")
        pane_label = _herdr_label(pane)
        want_label = pane_label_for(role) if role else ""
        if role:
            if not pane_label:
                self._refuse_live_pane("NO_PANE_LABEL")
            if pane_label != want_label and pane_label != role:
                self._refuse_live_pane("{}!={}".format(pane_label, want_label))
        pane_id = str(pane.get("pane_id") or "")
        tab_id = str(pane.get("tab_id") or "")
        child_id = (
            str(pane.get("workspace_id") or "")
            or workspace_of(tab_id)
            or workspace_of(pane_id)
        )
        if not child_id:
            self._refuse_live_pane("NO_CHILD_WORKSPACE_ID")
        try:
            child = self._workspace_record(child_id, environment)
        except _WorkspaceGone as exc:
            self._refuse_live_pane("child workspace_not_found", exc)
        if not self._worktree_is_linked(child):
            self._refuse_live_pane("CHILD_NOT_LINKED:{}".format(child_id))
        lane_id = str(spec.lane_label or spec.lane_key or "")
        parent_id = self._parent_workspace_id
        child_tokens = _herdr_tokens(child)
        pane_tokens = _herdr_tokens(pane)
        if parent_id:
            expected_lane = self._lane_identity_tokens(lane_id, parent_id)
            if self._identity_complete(expected_lane):
                if not child_tokens:
                    self._refuse_live_pane("LABEL_ONLY_LANE_WORKSPACE:{}".format(child_id))
                if not _tokens_match(child_tokens, expected_lane):
                    self._refuse_live_pane("LANE_TOKEN_MISMATCH:{}".format(child_id))
            if child_tokens.get(METADATA_TOKEN_PARENT) not in ("", parent_id):
                if child_tokens.get(METADATA_TOKEN_PARENT) != parent_id:
                    self._refuse_live_pane(
                        "{}!={}".format(
                            child_tokens.get(METADATA_TOKEN_PARENT), parent_id
                        )
                    )
        elif self._identity_complete(self._parent_identity_tokens()):
            parent_token = child_tokens.get(METADATA_TOKEN_PARENT) or ""
            if not parent_token:
                self._refuse_live_pane("NO_PARENT_TOKEN")
            parent_id = parent_token
            try:
                parent = self._workspace_record(parent_id, environment)
            except _WorkspaceGone:
                # The Space the child was opened under is gone (closed and
                # reopened by the operator) while the child and its agent
                # survived. The same self-heal as lane adoption: the child
                # must be listed under the parent resolved now, and is
                # re-pinned to it.
                parent_id = self._run_workspace(environment)
                try:
                    listed = self._child_listed_under(parent_id, child_id, environment)
                except _WorkspaceGone as exc:
                    self._refuse_live_pane("parent workspace_not_found", exc)
                if not listed:
                    self._refuse_live_pane(
                        "LANE_CHILD_NOT_UNDER_PARENT:{}:{}".format(child_id, parent_id)
                    )
                self._repin_lane(child_id, parent_id, environment)
                child_tokens = dict(child_tokens)
                child_tokens[METADATA_TOKEN_PARENT] = parent_id
                if pane_tokens.get(METADATA_TOKEN_PARENT):
                    pane_tokens = dict(pane_tokens)
                    pane_tokens[METADATA_TOKEN_PARENT] = parent_id
                parent = self._workspace_record(parent_id, environment)
            # The parent the child names must be a non-linked Space bound to
            # this repository; run tokens on it are not required (an
            # operator's Space carries none, a Maestro-created one may).
            if self._worktree_is_linked(parent):
                self._refuse_live_pane("PARENT_IS_LINKED_CHILD:{}".format(parent_id))
            if self._repository_root != Path():
                defect = self._parent_binding_defect(parent, self._repository_root)
                if defect:
                    self._refuse_live_pane(
                        "PARENT_NOT_REPO_BOUND:{}:{}".format(parent_id, defect)
                    )
            # The child's own identity is proven against the parent it
            # names, not only the pane's: a lane child retagged for another
            # run must not lend its pane to this one.
            expected_lane = self._lane_identity_tokens(lane_id, parent_id)
            if self._identity_complete(expected_lane) and not _tokens_match(
                child_tokens, expected_lane
            ):
                self._refuse_live_pane("LANE_TOKEN_MISMATCH:{}".format(child_id))
        if role and pane_tokens:
            expected_pane = self._pane_identity_tokens(
                lane_id, role, parent_id or self._parent_workspace_id
            )
            expected_pane.pop(METADATA_TOKEN_SCRATCH, None)
            if self._identity_complete(expected_pane) and not _tokens_match(
                pane_tokens, expected_pane
            ):
                self._refuse_live_pane("PANE_TOKEN_MISMATCH:{}".format(pane_id))
        if not tab_id:
            self._refuse_live_pane("NO_TAB_ID")
        if str(child.get("workspace_id") or child_id) != child_id:
            self._refuse_live_pane(child_id)

    def _register_adopted_role_layout(
        self,
        spec: LaunchSpec,
        pane_id: str,
        tab_id: str,
        workspace_id: str,
        environment: Mapping[str, str],
    ) -> None:
        if not workspace_id or not tab_id:
            self._refuse_live_pane("ROLE_LAYOUT_ID_MISSING")
        parent_id = self._parent_workspace_id
        try:
            child = self._workspace_record(workspace_id, environment)
        except _WorkspaceGone as exc:
            self._refuse_live_pane("child workspace_not_found", exc)
        if self._worktree_is_linked(child):
            parent_id = parent_id or _herdr_tokens(child).get(METADATA_TOKEN_PARENT, "")
        group = str(spec.lane_key or "")
        label = str(spec.lane_label or group)
        try:
            listed = self._herdr(
                "pane", "list", "--workspace", workspace_id, env=environment
            )
        except BaseException as exc:
            self._refuse_live_pane("pane list", exc)
        layout = self._validated_role_layout(
            tab_id,
            listed,
            parent_workspace_id=parent_id,
            child_workspace_id=workspace_id,
            lane_key=group,
            lane_label=label,
        )
        role = str(spec.pane_role or "")
        if role and layout.role_panes.get(role) not in (pane_id, None):
            if layout.role_panes.get(role) != pane_id:
                self._refuse_live_pane(
                    "{}!={}".format(pane_id, layout.role_panes.get(role))
                )
        if role and pane_id not in layout.panes:
            self._refuse_live_pane("ADOPTED_PANE_NOT_IN_LAYOUT")
        if role:
            layout.role_panes[role] = pane_id
        with self._handles_lock:
            if parent_id and not self._parent_workspace_id:
                self._parent_workspace_id = parent_id
                self._workspace_id = parent_id
            existing = self._tabs.get(group)
            if existing is not None and existing.tab_id != tab_id:
                self._refuse_live_pane("{}!={}".format(tab_id, existing.tab_id))
            if existing is not None and existing.child_workspace_id not in (
                "",
                workspace_id,
            ):
                self._refuse_live_pane(
                    "{}!={}".format(workspace_id, existing.child_workspace_id)
                )
            self._tabs[group] = layout

    def _handle_from_live_agent(
        self,
        spec: LaunchSpec,
        name: str,
        agent: Mapping[str, object],
        pane: Mapping[str, object],
        environment: Mapping[str, str],
    ) -> LaunchHandle:
        pane_id = str(pane.get("pane_id") or "")
        launched = Path(str(pane.get("cwd"))).resolve()
        tab_id = str(pane.get("tab_id") or "")
        child_id = (
            str(pane.get("workspace_id") or "")
            or workspace_of(tab_id)
            or workspace_of(pane_id)
        )
        parent_id = self._parent_workspace_id
        if child_id:
            try:
                child = self._workspace_record(child_id, environment)
                parent_id = parent_id or _herdr_tokens(child).get(
                    METADATA_TOKEN_PARENT, ""
                )
            except _WorkspaceGone:
                parent_id = parent_id
        transcript = _agent_transcript_path(agent, launched, environment)
        if transcript is None:
            transcript = wait_for_agent_transcript(
                lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
                name,
                0.0,
                launched_cwd=launched,
                environment=environment,
            )
        handle = LaunchHandle(
            spec.correlation_token,
            pane_id,
            name,
            launched,
            transcript_path=transcript,
            envelope_path=spec.envelope_path,
            environment=environment,
            workspace_id=child_id,
            tab_id=tab_id,
            lane_key=spec.lane_key,
            parent_workspace_id=parent_id,
            child_workspace_id=child_id,
            pane_role=str(spec.pane_role or ""),
            lane_label=str(spec.lane_label or spec.lane_key or ""),
        )
        with self._handles_lock:
            if parent_id and not self._parent_workspace_id:
                self._parent_workspace_id = parent_id
                self._workspace_id = parent_id
            self._handles[spec.correlation_token] = handle
            self._proven_absent.pop(spec.correlation_token, None)
            if transcript:
                self._tailers[spec.correlation_token] = TranscriptTailer(transcript)
        self._register_adopted_role_layout(
            spec, pane_id, tab_id, child_id, environment
        )
        self._register_role_handle(spec, handle)
        return handle


    def _reconnect_live_agent(
        self, spec: LaunchSpec, environment: Mapping[str, str]
    ) -> Optional[LaunchHandle]:
        """Adopt a still-running stable role agent after scheduler restart."""
        if not spec.pane_role:
            return None
        stable_name = _agent_name(spec.correlation_token)
        agent = self._fetch_agent(stable_name, environment)
        if agent is None:
            return None
        status = _agent_status_of(agent)
        if status not in REUSABLE_AGENT_STATUSES:
            self._refuse_live_pane(
                "AGENT_STATUS_UNOBSERVABLE:{}".format(status or "<missing>")
            )
        pane = self._prove_live_pane(spec, agent, environment)
        if (
            self.workspace_label
            and _herdr_tokens(pane).get(METADATA_TOKEN_SCRATCH)
            != METADATA_SCRATCH_REDIRECT
        ):
            return None
        actual = Path(str(pane.get("cwd"))).resolve()
        if actual != spec.worktree.resolve():
            self._refuse_live_pane("{}!={}".format(actual, spec.worktree.resolve()))
        return self._handle_from_live_agent(spec, stable_name, agent, pane, environment)

    def _parse_worktree_opened(self, payload: Mapping[str, object]) -> dict:
        workspace = _extract(payload, "workspace")
        tab = _extract(payload, "tab")
        root = _extract(payload, "root_pane")
        worktree = _extract(payload, "worktree")
        child_id = (
            workspace.get("workspace_id") if isinstance(workspace, dict) else None
        )
        tab_id = tab.get("tab_id") if isinstance(tab, dict) else None
        root_id = root.get("pane_id") if isinstance(root, dict) else None
        if not child_id or not tab_id or not root_id:
            raise LaunchRefused(LaunchRefusal.TAB_UNRESOLVED, "NO_CHILD_TAB_PANE")
        tab_ws = str(tab.get("workspace_id") or "") if isinstance(tab, dict) else ""
        root_ws = str(root.get("workspace_id") or "") if isinstance(root, dict) else ""
        root_tab = str(root.get("tab_id") or "") if isinstance(root, dict) else ""
        if tab_ws and tab_ws != str(child_id):
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_DRIFT,
                "{}!={}".format(tab_ws, child_id),
                pane_created=True,
            )
        if root_ws and root_ws != str(child_id):
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_DRIFT,
                "{}!={}".format(root_ws, child_id),
                pane_created=True,
            )
        if root_tab and root_tab != str(tab_id):
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "{}!={}".format(root_tab, tab_id),
                pane_created=True,
            )
        already_open = _extract(payload, "already_open") is True
        path = ""
        if isinstance(worktree, dict):
            path = str(worktree.get("path") or "")
        return {
            "child_workspace_id": str(child_id),
            "tab_id": str(tab_id),
            "root_pane_id": str(root_id),
            "already_open": already_open,
            "path": path,
            "workspace": workspace if isinstance(workspace, dict) else {},
        }

    def _worktree_open(
        self,
        parent_id: str,
        path: Path,
        label: str,
        environment: Mapping[str, str],
    ) -> dict:
        """Open a linked lane child under the parent Space (the operator's
        own, or the one Maestro created at the primary checkout)."""
        try:
            payload = self._herdr(
                "worktree",
                "open",
                "--workspace",
                parent_id,
                "--path",
                str(path),
                "--label",
                label,
                "--no-focus",
                env=environment,
            )
        except HerdrCallError as exc:
            if _herdr_error_code_of(exc) != WORKSPACE_NOT_FOUND:
                raise
            self._invalidate_workspace_layout(parent_id)
            raise _WorkspaceGone(str(exc)) from exc
        return self._parse_worktree_opened(payload)

    def _child_listed_under(
        self, parent_id: str, child_id: str, environment: Mapping[str, str]
    ) -> bool:
        """Whether `worktree list --workspace parent` holds `child` as open."""
        try:
            payload = self._herdr(
                "worktree", "list", "--workspace", parent_id, env=environment
            )
        except HerdrCallError as exc:
            if exc.code == WORKSPACE_NOT_FOUND:
                raise _WorkspaceGone(parent_id) from exc
            raise
        source = _extract(payload, "source")
        if isinstance(source, dict):
            source_id = str(source.get("source_workspace_id") or "")
            if source_id and source_id != parent_id:
                return False
        return any(
            str(item.get("open_workspace_id") or "") == child_id
            and item.get("is_linked_worktree") is True
            for item in _herdr_list(payload, "worktrees")
        )

    def _invalidate_lane_layout(self, child_workspace_id: str) -> None:
        if not child_workspace_id:
            return
        with self._handles_lock:
            for key, layout in list(self._tabs.items()):
                if layout.child_workspace_id == child_workspace_id:
                    self._tabs.pop(key, None)
                    for role_key in list(self._role_handles):
                        if role_key[0] == key:
                            self._role_handles.pop(role_key, None)

    def _invalidate_workspace_layout(self, workspace_id: str) -> None:
        """Release placement state whose workspace Herdr has proved absent."""
        if not workspace_id:
            return
        with self._handles_lock:
            parent_id = self._parent_workspace_id or self._workspace_id
            if workspace_id == parent_id:
                self._parent_workspace_id = ""
                self._workspace_id = ""
                self._seed_tab_id = ""
                self._tabs.clear()
                self._role_handles.clear()
                return
        self._invalidate_lane_layout(workspace_id)

    def _tab_for(
        self,
        spec: LaunchSpec,
        worktree: Path,
        env_flags: Sequence[str],
        environment: Mapping[str, str],
    ) -> _TabLayout:
        """The linked lane child this role's panes live in, created lazily."""
        del env_flags
        self._bind_run_identity(spec)
        group = spec.lane_key or ""
        with self._handles_lock:
            existing = self._tabs.get(group)
            if existing is not None and not existing.empty():
                return existing
            if not self.workspace_label:
                seed = self._split_parent(environment)
                layout = _TabLayout(tab_id="", panes=[seed], claimed=1)
                self._tabs[group] = layout
                return layout
            label = str(spec.lane_label or group)
            parent_id = self._run_workspace(environment)
            try:
                adopted = self._adopt_existing_lane(
                    parent_id, group, label, worktree, environment
                )
            except _WorkspaceGone as exc:
                # The parent this launcher resolved is gone (the operator
                # closed their Space). Every placement under it is released
                # and this launch refuses; the next one resolves the parent
                # afresh by the normal rule instead of inheriting a dead id.
                self._invalidate_workspace_layout(parent_id)
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_UNRESOLVED,
                    "PARENT_WORKSPACE_GONE:{}".format(exc),
                    pane_created=False,
                ) from exc
            if adopted is not None:
                self._tabs[group] = adopted
                return adopted
            # The lane's identity must be writable before the child exists;
            # a child that cannot be tagged must not be opened.
            lane_tokens = self._lane_identity_tokens(label, parent_id)
            _metadata_token_flags(lane_tokens)

            def open_child(current_parent: str) -> dict:
                try:
                    return self._worktree_open(
                        current_parent, worktree, label, environment
                    )
                except _WorkspaceGone:
                    current_parent = self._run_workspace(environment)
                    try:
                        return self._worktree_open(
                            current_parent, worktree, label, environment
                        )
                    except _WorkspaceGone as exc:
                        raise LaunchRefused(
                            LaunchRefusal.TAB_UNRESOLVED,
                            "workspace vanished twice: {0}".format(exc),
                        ) from exc

            try:
                opened = open_child(parent_id)
            except LaunchRefused:
                raise
            except BaseException as exc:
                raise LaunchRefused(
                    LaunchRefusal.TAB_UNRESOLVED,
                    "{0}: {1}".format(type(exc).__name__, exc),
                ) from exc
            child_id = str(opened["child_workspace_id"])
            if opened["already_open"]:
                # Another process opened this checkout between our listing
                # and our open. The workspace Herdr returned is the one that
                # holds exactly `--path worktree`, so an untagged one is ours
                # by path; it is tagged and adopted rather than refused, which
                # is the same rule `_adopt_existing_lane` applies.
                live = self._workspace_record(child_id, environment)
                tokens = _herdr_tokens(live)
                expected = lane_tokens
                if not tokens:
                    if not _same_resolved_path(
                        opened["path"], worktree
                    ) or not self._child_listed_under(
                        parent_id, child_id, environment
                    ):
                        raise LaunchRefused(
                            LaunchRefusal.BINDING_MISMATCH,
                            "LABEL_ONLY_LANE_WORKSPACE:{}".format(label),
                            pane_created=False,
                        )
                    self._tag_workspace(child_id, expected, environment)
                elif self._identity_complete(expected) and not _tokens_match(
                    tokens, expected
                ):
                    if not (
                        self._child_listed_under(parent_id, child_id, environment)
                        and self._lane_pinned_to_dead_parent(
                            child_id, tokens, expected, environment
                        )
                    ):
                        raise LaunchRefused(
                            LaunchRefusal.BINDING_MISMATCH,
                            "LANE_WORKSPACE_TOKEN_MISMATCH:{}".format(child_id),
                            pane_created=False,
                        )
                    self._repin_lane(child_id, parent_id, environment)
                layout = self._layout_from_child_workspace(
                    child_id, parent_id, group, label, environment
                )
                self._tabs[group] = layout
                return layout
            # `--workspace` names the parent we asked for; where Herdr
            # actually grouped the child is a separate fact, read back from
            # `worktree list --workspace parent` (`source_workspace_id` and
            # the child's presence). Should a second non-linked Space bound
            # to the repository appear between our lookup and our open,
            # which one Herdr treats as the source is undocumented. A child
            # placed elsewhere is closed and refused before it is ever
            # tagged as ours; tagging it would make every restart drift.
            try:
                under_parent = self._child_listed_under(
                    parent_id, child_id, environment
                )
            except _WorkspaceGone as exc:
                raise LaunchRefused(
                    LaunchRefusal.TAB_UNRESOLVED,
                    "parent vanished after open: {0}".format(exc),
                ) from exc
            if not under_parent:
                closed = True
                try:
                    self._close_workspace_absent_ok(child_id, environment)
                except HerdrCallError:
                    closed = False
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_DRIFT,
                    "LANE_CHILD_NOT_UNDER_PARENT:{}:{}{}".format(
                        child_id, parent_id, "" if closed else ":child close refused"
                    ),
                    pane_created=not closed,
                )
            self._tag_workspace(child_id, lane_tokens, environment)
            layout = _TabLayout(
                tab_id=str(opened["tab_id"]),
                panes=[str(opened["root_pane_id"])],
                claimed=0,
                parent_workspace_id=parent_id,
                child_workspace_id=child_id,
                lane_key=group,
                lane_label=label,
            )
            self._tabs[group] = layout
            self._close_seed_tab(environment)
            return layout

    def _acquire_pane(
        self,
        spec: LaunchSpec,
        worktree: Path,
        environment: Mapping[str, str],
    ) -> Tuple[str, _TabLayout, bool]:
        """Create a scratch-bound role pane lazily inside its linked lane child."""
        role_flags = pane_env_flags_for_role(worktree, environment)
        for recovery in range(2):
            layout = self._tab_for(spec, worktree, role_flags, environment)
            vanished = ""
            with layout.lock:
                role = str(spec.pane_role or "")
                replace_pane = ""
                try:
                    held = (
                        str(layout.role_panes.get(role) or "")
                        if role and role not in layout.refresh_roles
                        else ""
                    )
                    if held:
                        # A reuse is proven live, never taken from the
                        # in-memory grid alone: the pane may have been
                        # closed, or its child workspace with it, since it
                        # was adopted.
                        try:
                            self._herdr("pane", "get", held, env=environment)
                        except HerdrCallError as exc:
                            if _herdr_error_code_of(exc) != PANE_NOT_FOUND:
                                raise
                            layout.forget(held)
                            if layout.empty():
                                raise _WorkspaceGone(
                                    layout.child_workspace_id
                                    or workspace_of(layout.tab_id)
                                ) from exc
                    if role and role in layout.refresh_roles:
                        replace_pane = str(layout.role_panes.get(role) or "")
                        if not replace_pane:
                            raise LaunchRefused(
                                LaunchRefusal.BINDING_MISMATCH,
                                "STALE_ROLE_PANE_MISSING:{}".format(role),
                                pane_created=False,
                            )
                        try:
                            index = layout.panes.index(replace_pane)
                        except ValueError as exc:
                            raise LaunchRefused(
                                LaunchRefusal.BINDING_MISMATCH,
                                "STALE_ROLE_PANE_UNBOUND:{}".format(role),
                                pane_created=True,
                            ) from exc
                        parent_id = replace_pane
                        direction = "right"
                    elif role and layout.role_panes.get(role):
                        return str(layout.role_panes[role]), layout, True
                    elif layout.claimed == 0:
                        if not layout.panes or not layout.panes[0]:
                            raise LaunchRefused(
                                LaunchRefusal.TAB_UNRESOLVED, "NO_ROOT_PANE"
                            )
                        root_pane = str(layout.panes[0])
                        index = 0
                        parent_id = root_pane
                        direction = "right"
                        replace_pane = root_pane
                    else:
                        index = layout.claimed
                        declared = max(int(spec.pane_group_size or 0), index + 1)
                        layout.cols = max(layout.cols, grid_for(declared)[1])
                        # A grid slot is never adopted for a role. A pane
                        # that is in the grid but is no role's pane is
                        # unlabelled and untagged -- a split whose process
                        # stopped before labelling -- and its cwd is another
                        # role's checkout or nobody's. Adopting it by position
                        # renamed and tagged it before its cwd was proven, and
                        # the launch then refused BINDING_MISMATCH. The role
                        # splits its own pane; strays are closed once it
                        # exists (below), exactly as the root pane is.
                        while index < len(layout.panes) and layout.panes[index]:
                            index += 1
                        parent_index, direction = split_plan(index, layout.cols)
                        parent_id = layout.nearest_live(parent_index)
                        if parent_id is None:
                            raise LaunchRefused(
                                LaunchRefusal.TAB_UNRESOLVED, "NO_LIVE_PARENT"
                            )
                    split = self._herdr(
                        "pane",
                        "split",
                        parent_id,
                        "--direction",
                        direction,
                        "--cwd",
                        str(worktree),
                        "--no-focus",
                        *role_flags,
                        env=environment,
                    )
                except _WorkspaceGone as exc:
                    vanished = str(exc) or layout.child_workspace_id
                except HerdrCallError as exc:
                    code = _herdr_error_code_of(exc)
                    if code not in (WORKSPACE_NOT_FOUND, PANE_NOT_FOUND):
                        raise
                    # The split parent pane, or its workspace, went away
                    # between discovery and adoption. The lane layout is
                    # re-read once from Herdr rather than trusted.
                    vanished = layout.child_workspace_id or workspace_of(
                        layout.tab_id
                    ) or layout.parent_workspace_id
                else:
                    pane = _extract(split, "pane")
                    if not isinstance(pane, dict) or not pane.get("pane_id"):
                        raise LaunchRefused(
                            LaunchRefusal.NO_PANE, pane_created=True
                        )
                    pane_id = str(pane["pane_id"])
                    landed = (
                        str(pane.get("workspace_id") or "")
                        or workspace_of(str(pane.get("tab_id") or ""))
                        or workspace_of(pane_id)
                    )
                    if (
                        layout.child_workspace_id
                        and landed
                        and landed != layout.child_workspace_id
                    ):
                        closed = self._reap_pane(pane_id, environment)
                        raise LaunchRefused(
                            LaunchRefusal.WORKSPACE_DRIFT,
                            "{}!={}".format(landed, layout.child_workspace_id),
                            pane_created=not closed,
                        )
                    # The pane this split replaces goes first; then, inside
                    # a lane child, every pane that is no role's: the root
                    # pane `worktree open` seeded and any split whose process
                    # stopped before it was labelled. Each is unconfigured
                    # (no scratch contract) and at a cwd that is not this
                    # role's; leaving one behind is the orphan a later role
                    # would otherwise be handed by grid position.
                    unconfigured: List[str] = []
                    if replace_pane and replace_pane in layout.role_panes.values():
                        # A stale role pane is this role's by label; it is
                        # replaced whatever its cwd says.
                        unconfigured.append(replace_pane)
                    if layout.child_workspace_id:
                        owned = set(layout.role_panes.values())
                        scope = self._role_stage_worktree_scope(spec)
                        for stray in layout.panes:
                            if (
                                not stray
                                or stray == pane_id
                                or stray in owned
                                or stray in unconfigured
                            ):
                                continue
                            # An unlabelled pane is closed only when its cwd
                            # is inside this role's own checkout scope: the
                            # root pane the first role's open seeded, or this
                            # role's split that stopped before labelling. A
                            # pane at another role's cwd may be that role's
                            # split in flight in another process and is that
                            # role's to reap; a pane at a foreign or missing
                            # cwd is not Maestro's at all and is left alone.
                            if self._pane_cwd_within(str(stray), scope, environment):
                                unconfigured.append(str(stray))
                    if (
                        replace_pane
                        and replace_pane not in unconfigured
                        and index < len(layout.panes)
                        and layout.panes[index] == replace_pane
                    ):
                        # The pane we split from stays (another role's, or
                        # not ours); it keeps its grid slot.
                        index = len(layout.panes)
                    for stale in unconfigured:
                        if self._reap_pane(stale, environment):
                            continue
                        replacement_closed = self._reap_pane(pane_id, environment)
                        raise LaunchRefused(
                            LaunchRefusal.BINDING_MISMATCH,
                            "UNCONFIGURED_ROLE_PANE_NOT_CLOSED:{}".format(
                                role or stale
                            ),
                            pane_created=not replacement_closed,
                        )
                    while len(layout.panes) <= index:
                        layout.panes.append(None)
                    layout.panes[index] = pane_id
                    layout.claimed = max(layout.claimed, index + 1)
                    if role:
                        layout.role_panes[role] = pane_id
                        layout.refresh_roles.discard(role)
                    return pane_id, layout, False
            if not vanished:
                raise LaunchRefused(LaunchRefusal.TAB_UNRESOLVED, "NO_LIVE_PARENT")
            self._invalidate_workspace_layout(vanished)
            if recovery:
                raise LaunchRefused(
                    LaunchRefusal.TAB_UNRESOLVED,
                    "workspace vanished twice while splitting",
                )
        raise AssertionError("UNREACHABLE")

    def _label_pane(
        self, pane_id: str, spec: LaunchSpec, environment: Mapping[str, str]
    ) -> None:
        """Name a persistent role pane and prove the exact label stuck."""
        role = str(spec.pane_role or "")
        if role in PERSISTENT_PANE_ROLES or role in PANE_ROLE_LABELS:
            label = pane_label_for(role)
        else:
            attempt = (
                "a{}".format(spec.attempt_no) if spec.attempt_no is not None else ""
            )
            label = "-".join(part for part in (role, attempt) if part)
        if not label:
            return
        try:
            self._herdr("pane", "rename", pane_id, label, env=environment)
            payload = self._herdr("pane", "get", pane_id, env=environment)
            pane = _extract(payload, "pane")
            if not isinstance(pane, dict) or _herdr_label(pane) != label:
                raise RuntimeError("PANE_LABEL_UNCONFIRMED:{}".format(pane_id))
            parent_id = self._parent_workspace_id
            lane_id = str(spec.lane_label or spec.lane_key or "")
            self._tag_pane(
                pane_id,
                self._pane_identity_tokens(lane_id, role, parent_id),
                environment,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "pane rename {0}: {1}: {2}".format(pane_id, type(exc).__name__, exc),
                pane_created=True,
            ) from exc


    def _forget_pane(self, pane_id: str) -> None:
        """Drop a closed pane from its tab's grid.

        A closed pane cannot be split, so leaving it in the grid would let a
        later launch name a dead parent. The slot is emptied rather than
        removed, because the panes after it are still where they were.
        """
        with self._handles_lock:
            for layout in self._tabs.values():
                if layout.forget(pane_id):
                    return

    def _reap_pane(self, pane_id: str, environment: Mapping[str, str]) -> bool:
        """Close one pane a failed launch is about to abandon; say if it went.

        The return value is the whole point. Every post-split failure path in
        `launch` closes its pane and then has to tell the scheduler whether a
        pane still exists, and only this call knows. `True` iff herdr accepted
        the close; a close that raised, or that herdr refused, is reported as
        a pane that may still be there (§8.3: never report an absence nobody
        measured).
        """
        try:
            self._herdr("pane", "close", pane_id, env=environment)
        except HerdrCallError as exc:
            if exc.code not in (PANE_NOT_FOUND, WORKSPACE_NOT_FOUND):
                return False
            if exc.code == WORKSPACE_NOT_FOUND:
                self._invalidate_workspace_layout(workspace_of(pane_id))

            self._forget_pane(pane_id)
            return True
        except BaseException:
            return False
        self._forget_pane(pane_id)
        return True

    def _bind_handle_transcript(
        self, handle: LaunchHandle, transcript: Path
    ) -> None:
        """Attach a discovered transcript and its tailer to an owned handle."""
        path = Path(transcript)
        object.__setattr__(handle, "transcript_path", path)
        with self._handles_lock:
            self._tailers[handle.correlation_token] = TranscriptTailer(path)

    def _runtime_submission_recorded(
        self, handle: LaunchHandle, prompt_path: Path
    ) -> Callable[[], bool]:
        """Lane-path proof for one prompt offer.

        A known transcript requires a rising prompt-path record so a previous
        turn cannot stand in as this offer's proof. A missing transcript is
        unproven False, never a refusal: query the owned agent, attach a
        newly discovered path and tailer atomically, then check marks.
        Herdr query failures propagate so `submit_agent_prompt` records them
        as diagnostic proof-probe failures.
        """
        if handle.transcript_path is not None:
            return _rising_submission_record(handle, prompt_path)

        def recorded() -> bool:
            if handle.transcript_path is None:
                payload = self._herdr(
                    "agent",
                    "get",
                    handle.agent_name,
                    env=handle.environment,
                    timeout=15.0,
                )
                found = _agent_transcript_path(
                    _agent_record(payload),
                    handle.launched_cwd,
                    handle.environment,
                )
                if found is not None:
                    self._bind_handle_transcript(handle, found)
            if handle.transcript_path is None:
                return False
            return prompt_submission_marks(handle, prompt_path) > 0

        return recorded

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        if not self.admitted_routes.admits(spec.route):
            raise LaunchRefused(LaunchRefusal.ROUTE_NOT_ADMITTED, spec.route)
        self._bind_run_identity(spec)
        # B13 and route argv validation both happen before anything is created.
        # An overflowing agent compaction-loops and answers about another task;
        # an invalid immutable launch spec cannot become valid on retry.
        prepare_route_prompt(spec)
        preflight_launch_prompt(spec)
        if spec.route == "omp":

            try:
                route_argv = build_omp_argv(self.omp_path, spec)
            except ValueError as exc:
                if str(exc) != "OMP_PROFILE_REQUIRED":
                    raise
                raise LaunchRefused(
                    LaunchRefusal.OMP_PROFILE_REQUIRED, spec.route
                ) from exc
        elif spec.route == "claude":
            route_argv = build_claude_argv(self.claude_path, spec)
        else:
            raise LaunchRefused(LaunchRefusal.UNSUPPORTED_ROUTE, spec.route)
        worktree = spec.worktree.resolve()
        self.provision(worktree)
        environment = MappingProxyType(dict(spec.environment))
        # The pane shell is forked by the herdr server, not by the CLI process
        # below, so `env=` alone leaves the bracket's redirection outside the
        # pane entirely (§8.3). `--env` is the only surface that crosses.
        #
        # Built before the parent pane is resolved, and the order is the
        # point: `pane_env_flags` refuses an incomplete redirection, and
        # SCRATCH_REDIRECT_MISSING declares `pane_created=False` because it is
        # raised before herdr is called *at all*. Resolving the parent first
        # would make one herdr call before that refusal and falsify it.
        pane_env_flags(environment)
        def resubmit_adopted(adopted: LaunchHandle) -> LaunchHandle:
            if spec.prepare_adopted_cwd is not None:
                self._verified_handle_binding(adopted)
                spec.prepare_adopted_cwd(adopted.launched_cwd)
                # The callback may rewrite the prompt with the adopted CWD.
                prepare_route_prompt(spec)
                preflight_launch_prompt(spec)
            return self.resubmit(
                adopted,
                spec.prompt_path,
                route=spec.route,
                envelope_path=spec.envelope_path,
            )

        existing = self._validated_existing_role_handle(spec, environment)
        if existing is None:
            existing = self._reconnect_live_agent(spec, environment)
        if existing is not None:
            return resubmit_adopted(existing)
        # Placement: the operator's (or Maestro-created) parent Space, a
        # linked lane child under it, a lazy role pane inside the child.
        pane_id, layout, reused_role_pane = self._acquire_pane(
            spec, worktree, environment
        )
        if reused_role_pane:
            # A role agent is started only when the stable exact agent is
            # absent. The lookup above ran before the pane was resolved; a
            # concurrent dispatch of this same role may have started the
            # agent in this very pane since, and starting a second one would
            # be refused `agent_pane_busy` and reap the live pane.
            existing = self._reconnect_live_agent(spec, environment)
            if existing is not None:
                return resubmit_adopted(existing)
        with layout.lock:
            pane_is_in_layout = pane_id in layout.panes
            placement_tab_id = layout.tab_id
            child_workspace_id = layout.child_workspace_id
            parent_workspace_id = layout.parent_workspace_id or self._parent_workspace_id
        if not pane_is_in_layout:
            closed = self._reap_pane(pane_id, environment)
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "ACQUIRED_PANE_NOT_IN_LAYOUT",
                pane_created=not closed,
            )
        placement_workspace_id = (
            child_workspace_id
            or workspace_of(placement_tab_id)
            or workspace_of(pane_id)
        )
        landed = workspace_of(pane_id)
        if placement_workspace_id and landed and landed != placement_workspace_id:
            closed = self._reap_pane(pane_id, environment)
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_DRIFT,
                "{0}!={1}".format(landed, placement_workspace_id),
                pane_created=not closed,
            )
        if (
            parent_workspace_id
            and placement_workspace_id
            and placement_workspace_id == parent_workspace_id
            and self.workspace_label
        ):
            closed = self._reap_pane(pane_id, environment)
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_DRIFT,
                "pane landed in parent run workspace {}".format(parent_workspace_id),
                pane_created=not closed,
            )

        self._label_pane(pane_id, spec, environment)
        name = _agent_name(spec.correlation_token)
        current = self._herdr("pane", "get", pane_id, env=environment)
        bound = _extract(current, "pane")
        if placement_tab_id:
            bound_tab_id = str(bound.get("tab_id") if isinstance(bound, dict) else "")
            if bound_tab_id != placement_tab_id:
                closed = self._reap_pane(pane_id, environment)
                raise LaunchRefused(
                    LaunchRefusal.TAB_UNRESOLVED,
                    "{}!={}".format(bound_tab_id, placement_tab_id),
                    pane_created=not closed,
                )
            if workspace_of(bound_tab_id) != placement_workspace_id:
                closed = self._reap_pane(pane_id, environment)
                raise LaunchRefused(
                    LaunchRefusal.TAB_UNRESOLVED,
                    "{}!={}".format(workspace_of(bound_tab_id), placement_workspace_id),
                    pane_created=not closed,
                )
        actual = (
            Path(str(bound.get("cwd"))).resolve()
            if isinstance(bound, dict) and bound.get("cwd")
            else None
        )
        if actual != worktree:
            closed = self._reap_pane(pane_id, environment)
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "{}!={}".format(actual, worktree),
                pane_created=not closed,
            )
        bound_cwd = actual or worktree
        if reused_role_pane and spec.prepare_adopted_cwd is not None:
            spec.prepare_adopted_cwd(bound_cwd)
            # The callback may rewrite the prompt with the retained role CWD.
            prepare_route_prompt(spec)
            preflight_launch_prompt(spec)
        try:
            _wait_for_available_shell(
                lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
                pane_id,
            )
            started = _start_agent_when_free(
                lambda: self._herdr(
                    "agent",
                    "start",
                    name,
                    "--kind",
                    spec.route,
                    "--pane",
                    pane_id,
                    "--timeout",
                    "180000",
                    "--",
                    *route_argv[1:],
                    env=environment,
                    timeout=185.0,
                ),
                window_s=self.agent_start_busy_window_s,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                # KeyboardInterrupt/SystemExit are not launch outcomes.
                raise
            code = _herdr_error_code_of(exc) if isinstance(exc, HerdrCallError) else ""
            if code == AGENT_NOT_READY:
                # The agent is in the pane and blocked during startup; Herdr
                # keeps its name registered. Closing the pane would kill a
                # running agent and the next attempt would block the same
                # way. The pane stays, and the next dispatch reconnects to
                # the stable name once the agent is idle.
                raise LaunchRefused(
                    LaunchRefusal.AGENT_START_REFUSED,
                    "{0}:{1}".format(AGENT_NOT_READY, name),
                    pane_created=True,
                ) from exc
            if self._stable_agent_in_pane(name, pane_id, environment):
                # A concurrent dispatch of this role won the start in this
                # very pane (Herdr refused ours `agent_pane_busy`). The pane
                # and the agent are the role's now, whichever launch split
                # the pane; reaping it would kill the live agent.
                raise LaunchRefused(
                    LaunchRefusal.AGENT_START_REFUSED,
                    "STABLE_AGENT_ALREADY_PRESENT:{}".format(name),
                    pane_created=False,
                ) from exc
            # Reap first, then state what the reap achieved. Re-raising
            # herdr's own `HerdrCallError` from here was the 2026-08-18
            # defect: it is not a `LaunchRefused`, so `LaunchFailed`'s
            # fail-closed `pane_created` said a pane survived a close that had
            # just succeeded, the scheduler quiesced an attempt whose handle
            # was never registered, and PROCESS_GROUP_UNTRACKED replaced a
            # retryable launch failure with a terminal QUIESCENCE_UNPROVEN.
            closed = self._reap_pane(pane_id, environment)
            raise LaunchRefused(
                LaunchRefusal.AGENT_START_REFUSED,
                "{0}: {1}".format(type(exc).__name__, exc),
                pane_created=not closed,
            ) from exc
        current = self._herdr("pane", "get", pane_id, env=environment)
        bound = _extract(current, "pane")
        actual = (
            Path(str(bound.get("cwd"))).resolve()
            if isinstance(bound, dict) and bound.get("cwd")
            else None
        )
        if actual != worktree and not reused_role_pane:
            # An agent is running in this pane, so the reap is `cancel`'s
            # (process group first, then the pane) rather than a bare close.
            # It raises `HarnessQuiescenceError` when it cannot finish, which
            # is the one case here that must not be restated as a refusal:
            # something is still owned and the caller has to know.
            self.cancel(
                LaunchHandle(
                    spec.correlation_token,
                    pane_id,
                    name,
                    actual or Path("/"),
                    environment=environment,
                ),
                time.monotonic() + 1.0,
            )
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "{}!={}".format(actual, worktree),
                pane_created=False,
            )
        bound_cwd = actual or worktree
        agent = _extract(started, "agent")
        transcript = _agent_transcript_path(agent, bound_cwd, environment)
        handle = LaunchHandle(
            spec.correlation_token,
            pane_id,
            name,
            bound_cwd,
            transcript_path=transcript,
            envelope_path=spec.envelope_path,
            environment=environment,
            workspace_id=placement_workspace_id,
            tab_id=placement_tab_id,
            lane_key=spec.lane_key,
            parent_workspace_id=parent_workspace_id,
            child_workspace_id=child_workspace_id or placement_workspace_id,
            pane_role=str(spec.pane_role or ""),
            lane_label=str(spec.lane_label or spec.lane_key or ""),
        )

        with self._handles_lock:
            self._handles[spec.correlation_token] = handle
            self._proven_absent.pop(spec.correlation_token, None)
            if transcript:
                self._tailers[spec.correlation_token] = TranscriptTailer(transcript)
        self._register_role_handle(spec, handle)
        try:
            # Every coding route receives the complete node instruction only
            # after its composer is ready. Startup delivery can race agent
            # initialization and execute only the prompt's leading command.
            wait_for_interactive_agent(
                lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
                name,
            )
            # The trailing space is load-bearing, not cosmetic. `@` opens the
            # composer's file-path completion popup in both omp and Claude
            # Code, and while that popup is open it consumes Enter to accept a
            # completion instead of submitting the message -- so the Enter
            # `agent prompt` sends atomically with the text is swallowed and
            # the prompt sits on screen, composed and unsent. A space
            # terminates the path token, which closes the popup, so the Enter
            # reaches the composer. This is the 2026-08-27 stall: a grok
            # builder held `@<prompt>` at revision 1 for an hour, and a single
            # Enter typed by hand afterwards started the turn immediately.
            bootstrap = "@{0} ".format(spec.prompt_path.resolve())

            # Claude remote-control sessions, and some OMP Herdr records, do
            # not expose a JSONL path until the first prompt is accepted —
            # and some backends never expose one. Waiting for that file
            # before or after offering is a circular refusal. Discover it
            # through the proof predicate while the submission loop runs;
            # missing stays unproven False.
            submission_recorded = self._runtime_submission_recorded(
                handle, spec.prompt_path
            )
            # Durable pane identity is recorded before prompt delivery. A
            # Claude transcript may not exist yet; the callback must not turn
            # that route-owned file-creation order into an orphaned pane.
            if spec.on_identity is not None:
                spec.on_identity(handle)
            submit_agent_prompt(
                lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
                pane_id,
                bootstrap,
                name,
                timeout_s=60.0,
                working_proves=True,
                # The lane path never convicts here. Turn length is unbounded
                # (§7.6), so the end of this function's budget is a fact about
                # the budget; the node's liveness and quiescence machinery is
                # what adjudicates a lane attempt.
                refuse_unproven=False,
                submission_recorded=submission_recorded,
            )

            # The pane's foreground group is meaningful only after submission.
            liveness_pid = pane_liveness_pid(
                lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
                pane_id,
            )
            if liveness_pid is not None:
                object.__setattr__(handle, "liveness_pid", liveness_pid)
            return handle
        except BaseException as exc:
            # Submission proof is only an observation of the launch path. The
            # attempt's own terminal declaration outranks it, exactly as it
            # outranks stale pane status in `poll`. A fast agent can accept,
            # finish, and write its envelope while Herdr's revision meter
            # remains static; cancelling here would discard completed work and
            # relaunch the original assignment from its base commit.
            if self._declared_result(handle) is not None:
                return handle
            # No declaration exists, so a started agent is still owned
            # execution. Cancel it before returning a launch refusal;
            # cancellation failure remains a quiescence error.
            self.cancel(handle, time.monotonic() + 5.0)
            if not isinstance(exc, Exception):
                raise
            raise LaunchRefused(
                LaunchRefusal.PROMPT_SUBMISSION_REFUSED,
                "{0}: {1}".format(type(exc).__name__, exc),
                pane_created=False,
            ) from exc

    def _verified_handle_binding(self, handle: LaunchHandle) -> None:
        """Prove a registered handle still names its pane, actor, and cwd.

        A resubmission must never follow a label or an in-memory convenience
        map alone.  Herdr ids plus the pane cwd are the ownership proof; a
        mismatch means a replacement session may have reused display text and
        must not receive this lane's repair prompt.
        """
        token = str(handle.correlation_token or "")
        if not token or handle.agent_name != _agent_name(token):
            raise LaunchRefused(LaunchRefusal.BINDING_MISMATCH, token)
        with self._handles_lock:
            if self._handles.get(token) is not handle:
                raise LaunchRefused(LaunchRefusal.BINDING_MISMATCH, token)
        try:
            pane_payload = self._herdr(
                "pane", "get", handle.pane_id, env=handle.environment
            )
        except HerdrCallError as exc:
            if exc.code in (AGENT_NOT_FOUND, PANE_NOT_FOUND, WORKSPACE_NOT_FOUND):
                raise LaunchRefused(
                    LaunchRefusal.BINDING_MISMATCH, handle.pane_id
                ) from exc
            raise
        pane = _extract(pane_payload, "pane")
        if (
            not isinstance(pane, dict)
            or str(pane.get("pane_id") or "") != handle.pane_id
        ):
            raise LaunchRefused(LaunchRefusal.BINDING_MISMATCH, handle.pane_id)
        cwd = pane.get("cwd")
        actual = Path(str(cwd)).resolve() if cwd else None
        if actual != handle.launched_cwd.resolve():
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "{}!={}".format(actual, handle.launched_cwd),
            )
        landed = (
            str(pane.get("workspace_id") or "")
            or workspace_of(str(pane.get("tab_id") or ""))
            or workspace_of(handle.pane_id)
        )
        if handle.child_workspace_id and landed and landed != handle.child_workspace_id:
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_DRIFT,
                "{}!={}".format(landed, handle.child_workspace_id),
            )
        if handle.tab_id:
            bound_tab = str(pane.get("tab_id") or "")
            if bound_tab and bound_tab != handle.tab_id:
                raise LaunchRefused(
                    LaunchRefusal.BINDING_MISMATCH,
                    "{}!={}".format(bound_tab, handle.tab_id),
                )

        try:
            agent_payload = self._herdr(
                "agent", "get", handle.agent_name, env=handle.environment
            )
        except HerdrCallError as exc:
            if exc.code == AGENT_NOT_FOUND:
                raise LaunchRefused(
                    LaunchRefusal.BINDING_MISMATCH, handle.agent_name
                ) from exc
            raise
        agent = _agent_record(agent_payload)
        if not isinstance(agent, dict) or not _agent_named(agent, handle.agent_name):
            raise LaunchRefused(LaunchRefusal.BINDING_MISMATCH, handle.agent_name)
        agent_pane = agent.get("pane_id")
        if agent_pane is not None and str(agent_pane) != handle.pane_id:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "{}!={}".format(agent_pane, handle.pane_id),
            )

    def resubmit(
        self,
        handle: LaunchHandle,
        prompt_path: Path,
        *,
        route: str = "",
        expected_token: Optional[str] = None,
        timeout_s: float = 60.0,
        envelope_path: Optional[Path] = None,
    ) -> LaunchHandle:
        """Submit one new prompt to an already-owned interactive actor.

        The original pane, actor name, correlation token, and worktree binding
        are re-read before calling Herdr.  This deliberately returns the
        existing handle: a correction cycle has one actor session, rather than
        a sequence of visually similar replacement panes.
        """
        if expected_token is not None and handle.correlation_token != expected_token:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH,
                "{}!={}".format(handle.correlation_token, expected_token),
            )
        prompt = Path(prompt_path)
        if not prompt.is_file():
            raise LaunchRefused(LaunchRefusal.PROMPT_UNMEASURED, str(prompt))
        if route:
            text = prompt.read_text(encoding="utf-8")
            prepared = prepare_route_prompt_text(route, text)
            if prepared != text:
                prompt.write_text(prepared, encoding="utf-8")
        if envelope_path is not None:
            object.__setattr__(handle, "envelope_path", Path(envelope_path))
        self._verified_handle_binding(handle)
        wait_for_interactive_agent(
            lambda *args, **kwargs: self._herdr(
                *args, env=handle.environment, **kwargs
            ),
            handle.agent_name,
        )
        submit_agent_prompt(
            lambda *args, **kwargs: self._herdr(
                *args, env=handle.environment, **kwargs
            ),
            handle.pane_id,
            # Trailing space closes the `@` completion popup so the Enter is
            # not consumed by it -- see the bootstrap in `launch`.
            "@{} ".format(prompt.resolve()),
            handle.agent_name,
            timeout_s=timeout_s,
            working_proves=True,
            # A correction cycle is the same lane path as the first launch and
            # gets the same answer: offered, unproven, adjudicated downstream.
            refuse_unproven=False,
            submission_recorded=self._runtime_submission_recorded(
                handle, prompt
            ),
        )
        # A new prompt is a new turn. Any confirmation window still open from
        # the previous one measures a pane that has since been handed work,
        # and would convict this turn on the last one's silence.
        self._clear_quiescence(handle.correlation_token)
        return handle


    def wait_for_idle(self, handle: LaunchHandle, timeout_s: float = 60.0) -> None:
        """Wait for a completed turn to return to its retained composer.

        The envelope is written before the coding agent finishes rendering its
        final response. A one-shot status read therefore races ``working`` even
        though the declaration is complete. Herdr's bounded lifecycle wait is
        the authority here; the pane remains open for the correction loop.
        """
        self._verified_handle_binding(handle)
        wait_for_interactive_agent(
            lambda *args, **kwargs: self._herdr(
                *args, env=handle.environment, **kwargs
            ),
            handle.agent_name,
            timeout_s=timeout_s,
        )

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
            payload = self._herdr(
                "agent", "get", handle.agent_name, env=handle.environment
            )
        except RuntimeError:
            return None
        agent = _agent_record(payload)
        if not isinstance(agent, dict):
            return None
        raw = _agent_status_of(agent)
        return raw or None

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
            payload = self._herdr(
                "agent", "get", handle.agent_name, env=handle.environment
            )
        except HerdrCallError as exc:
            # An agent whose pane is closed is not reported as an agent with no
            # record: herdr exits nonzero with `agent_not_found`. Reading that
            # as an error rather than as absence makes `cancel` treat a
            # successful close as unproven quiescence, which blocks every agent
            # node. Keyed on the typed code, never the message (§1.2).
            if exc.code != AGENT_NOT_FOUND:
                raise
            return True
        return _agent_record(payload) is None

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
            return PollResult(
                PollState.EXITED,
                0 if success else 1,
                "ENVELOPE_SUCCESS" if success else "ENVELOPE_FAILURE",
            )
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
            payload = self._herdr(
                "agent", "get", handle.agent_name, env=handle.environment
            )
        except HerdrCallError as exc:
            if exc.code != AGENT_NOT_FOUND:
                raise
            return PollResult(PollState.GONE, detail="AGENT_GONE")
        agent = _agent_record(payload)
        if not isinstance(agent, dict):
            return PollResult(PollState.GONE, detail="AGENT_GONE")
        status = _agent_status_of(agent) or "unknown"
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
            self._clear_quiescence(handle.correlation_token)
            return PollResult(PollState.STARTING)
        # Idle after at least one turn and still no envelope: the agent may
        # have finished its turn without writing one. That is a failed
        # attempt, not a running one -- waiting would hold the node until its
        # timeout with nothing left to observe.
        #
        # But a *single* `idle` sample cannot say that. `idle` is herdr's
        # answer for the gap between a tool result landing and the next
        # assistant message starting, and for a pane blocked inside a tool
        # call, exactly as it is for a composer whose turn is over. Reading
        # one sample as the end of the turn convicted a live builder on
        # run-8d1a71f463e4430f92a125a8f8b3731d: `lane-acquisition-manifest`'s
        # generation-3 repair was scored EXITED/NO_ENVELOPE at 15:55:33Z in
        # the 14-second gap before its next message, the scheduler's
        # `repair-idle` quiescence proof then correctly observed the same
        # agent still working and timed out, and the lane ended terminal
        # QUIESCENCE_UNPROVEN -- 75 seconds before that agent wrote a complete
        # envelope. Poll and the quiescence proof read the same pane seconds
        # apart and disagreed; the proof was right.
        #
        # So the idle has to *hold*, over `quiescence_confirm_s`, with no
        # transcript record appearing inside it -- B14's rule, and the same
        # one `FinalizationWindow.poll` applies to a reviewer. Any record, and
        # any status that is not `idle`, restarts the confirmation. The
        # envelope still outranks all of it: `_declared_result` ran at the top
        # of this method, so a turn that declares mid-window ends here as its
        # own declaration rather than as a stop.
        if status == AGENT_QUIESCENT_STATUS and (turns or tailer is None):
            if self._quiescence_confirmed(handle.correlation_token, turns):
                return PollResult(PollState.EXITED, 1, "NO_ENVELOPE")
            return PollResult(PollState.RUNNING)
        self._clear_quiescence(handle.correlation_token)
        return PollResult(PollState.RUNNING)

    def _quiescence_confirmed(self, token: str, turns: int) -> bool:
        """Whether `idle` has held long enough to mean the turn stopped.

        Latching rather than sampling. The first `idle` poll of a run opens
        the window and answers `False`; a later poll answers `True` only once
        the window has been open longer than `quiescence_confirm_s` *and* the
        transcript has not grown since it opened. A record appearing inside
        the window reopens it from that record, because growth is positive
        evidence of work and outranks the pane's status either way.
        """
        now = time.monotonic()
        with self._handles_lock:
            latched = self._quiescent_since.get(token)
            if latched is None or turns > latched[1]:
                self._quiescent_since[token] = (now, turns)
                return False
            return (now - latched[0]) > self.quiescence_confirm_s

    def _clear_quiescence(self, token: str) -> None:
        """Drop any open confirmation window; the actor is not quiescent."""
        with self._handles_lock:
            self._quiescent_since.pop(token, None)

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
                    "HERDR_QUIESCENCE_UNPROVEN:{}".format(token)
                ) from exc
            if not _process_group_absent(handle.process_group):
                raise HarnessQuiescenceError(
                    "HERDR_QUIESCENCE_UNPROVEN:{}".format(token)
                )
        try:
            # herdr confirms a close as `{"result": {"type": "ok"}}`; there is
            # no `closed` flag. Demanding one turned every successful close
            # into PANE_CLOSE_UNCONFIRMED, which is raised inside the block
            # that proves quiescence, so the proof could never succeed.
            response = self._herdr(
                "pane", "close", handle.pane_id, env=handle.environment
            )
            result = response.get("result")
            closed = _extract(response, "closed")
            confirmed = closed is True or (
                isinstance(result, dict) and result.get("type") == "ok"
            )
            if not confirmed:
                raise RuntimeError("PANE_CLOSE_UNCONFIRMED:{}".format(handle.pane_id))
            # `_agent_absent`, not `poll`: a successful attempt leaves an
            # envelope, and `poll` now reports that declaration in preference
            # to any observation of the pane. Asking `poll` here would read a
            # success as EXITED, conclude the pane was still live, and refuse
            # quiescence for every node that worked.
            if not self._agent_absent(handle):
                raise RuntimeError("PANE_STILL_LIVE:{}".format(handle.pane_id))
        except BaseException as exc:
            raise HarnessQuiescenceError(
                "HERDR_QUIESCENCE_UNPROVEN:{}".format(token)
            ) from exc
        self._forget_pane(handle.pane_id)
        with self._handles_lock:
            if self._handles.get(token) is handle:
                self._handles.pop(token)
                self._tailers.pop(token, None)
                self._proven_absent[token] = handle
            for key, held in list(self._role_handles.items()):
                if held is handle:
                    self._role_handles.pop(key)

    def agent_presence(self, token: str) -> Optional[bool]:
        """Whether Herdr still holds the deterministic agent for an attempt.

        `False` requires Herdr's typed `agent_not_found`; transport failure is
        `None`, never absence. This is intentionally independent of the
        process-local handle registry so a later `run resume` invocation can
        prove that an attempt blocked on unproven quiescence is now gone.
        """
        try:
            payload = self._herdr("agent", "get", _agent_name(token))
        except HerdrCallError as exc:
            return False if exc.code == AGENT_NOT_FOUND else None
        except RuntimeError:
            return None
        return True if _agent_record(payload) is not None else None

    def retain(self, handle: LaunchHandle) -> None:
        """Keep a completed turn's pane/session for a later correction cycle."""
        self._verified_handle_binding(handle)

    def _session_name(self, handle: LaunchHandle, project_identity: str) -> str:
        lane = str(handle.lane_label or handle.lane_key or "")
        role = str(handle.pane_role or "")
        if project_identity and self._run_id:
            return session_name_for(project_identity, self._run_id, lane, role)
        if self.workspace_label:
            return "{}-{}-{}".format(
                self.workspace_label, lane, pane_label_for(role)
            )
        return session_name_for("maestro", handle.correlation_token, lane, role)

    def _pane_has_text(
        self, handle: LaunchHandle, needle: str
    ) -> bool:
        try:
            payload = self._herdr(
                "pane",
                "read",
                handle.pane_id,
                "--source",
                "recent-unwrapped",
                env=handle.environment,
            )
        except BaseException:
            return False
        result = payload.get("result", payload)
        text = ""
        if isinstance(result, dict):
            text = str(result.get("text") or "")
        return needle in text

    def _confirm_session_rename(
        self, handle: LaunchHandle, session_name: str, timeout_s: float
    ) -> None:
        confirm = session_rename_confirmation(session_name)
        if self._pane_has_text(handle, confirm):
            return
        try:
            self._herdr(
                "pane",
                "send-text",
                handle.pane_id,
                "/rename {}".format(session_name),
                env=handle.environment,
            )
            self._herdr(
                "pane",
                "send-keys",
                handle.pane_id,
                "enter",
                env=handle.environment,
            )
            self._herdr(
                "pane",
                "wait-output",
                handle.pane_id,
                "--match",
                confirm,
                "--timeout",
                str(max(1, int(timeout_s * 1000))),
                env=handle.environment,
                timeout=timeout_s + 5.0,
            )
        except BaseException as exc:
            if isinstance(exc, LaunchRefused):
                raise
            raise LaunchRefused(
                LaunchRefusal.SESSION_RENAME_UNCONFIRMED,
                "{0}: {1}: {2}".format(
                    handle.pane_id, type(exc).__name__, exc
                ),
                pane_created=True,
            ) from exc


    def _close_workspace_absent_ok(
        self, workspace_id: str, environment: Mapping[str, str]
    ) -> None:
        if not workspace_id or workspace_id in self._cleaned_absent:
            return
        try:
            self._herdr("workspace", "close", workspace_id, env=environment)
        except HerdrCallError as exc:
            if exc.code == WORKSPACE_NOT_FOUND:
                self._cleaned_absent.add(workspace_id)
                self._invalidate_workspace_layout(workspace_id)
                return
            raise
        self._cleaned_absent.add(workspace_id)
        self._invalidate_workspace_layout(workspace_id)

    def complete_run(
        self,
        handles: Sequence[LaunchHandle],
        *,
        project_identity: str = "",
        timeout_s: float = 60.0,
    ) -> None:
        """Rename idle role sessions, then close the lane children.

        The parent Space is never closed: it is the operator's own Space when
        one was open on the repository, and even a Maestro-created one is
        shared by every run on that repository. Cancel is not this path. A
        rename that is not confirmed leaves every affected pane and lane
        workspace open.
        """
        ordered: List[LaunchHandle] = []
        seen: set[str] = set()
        for handle in handles:
            token = handle.correlation_token
            if token in seen:
                continue
            seen.add(token)
            ordered.append(handle)
        confirmed: List[LaunchHandle] = []
        for handle in ordered:
            if handle.pane_id in self._cleaned_absent:
                continue
            try:
                self.wait_for_idle(handle, timeout_s=timeout_s)
            except HerdrCallError as exc:
                if exc.code in (PANE_NOT_FOUND, WORKSPACE_NOT_FOUND, AGENT_NOT_FOUND):
                    self._cleaned_absent.add(handle.pane_id)
                    continue
                raise LaunchRefused(
                    LaunchRefusal.SESSION_RENAME_UNCONFIRMED,
                    "{0}: {1}".format(type(exc).__name__, exc),
                    pane_created=True,
                ) from exc
            except LaunchRefused:
                raise
            name = self._session_name(handle, project_identity)
            self._confirm_session_rename(handle, name, timeout_s)
            confirmed.append(handle)
        children: List[str] = []
        environments: Dict[str, Mapping[str, str]] = {}
        for handle in confirmed:
            child = handle.child_workspace_id or handle.workspace_id
            if child and child not in children:
                children.append(child)
                environments[child] = handle.environment
            self._forget_pane(handle.pane_id)
            self._cleaned_absent.add(handle.pane_id)
        for child in children:
            if child == self._parent_workspace_id:
                # A handle without a child id names the parent by fallback;
                # the parent is never closed.
                continue
            self._close_workspace_absent_ok(child, environments.get(child, {}))

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
        self.retained: List[str] = []
        self.completed: List[Tuple[str, ...]] = []

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        handle = LaunchHandle(
            spec.correlation_token,
            "fake:" + spec.correlation_token,
            _agent_name(spec.correlation_token),
            spec.worktree.resolve(),
            envelope_path=spec.envelope_path,
            parent_workspace_id="",
            child_workspace_id="",
            pane_role=str(spec.pane_role or ""),
            lane_key=spec.lane_key,
            lane_label=str(spec.lane_label or spec.lane_key or ""),
        )
        self._handles[spec.correlation_token] = handle
        self._states[spec.correlation_token] = PollResult(PollState.RUNNING)
        return handle

    def resubmit(
        self,
        handle: LaunchHandle,
        prompt_path: Path,
        *,
        route: str = "",
        expected_token: Optional[str] = None,
        timeout_s: float = 60.0,
        envelope_path: Optional[Path] = None,
    ) -> LaunchHandle:
        del route, timeout_s
        if expected_token is not None and expected_token != handle.correlation_token:
            raise LaunchRefused(LaunchRefusal.PROMPT_UNMEASURED)
        if self._handles.get(handle.correlation_token) is not handle:
            raise LaunchRefused(LaunchRefusal.PROMPT_UNMEASURED)
        if not Path(prompt_path).is_file():
            raise LaunchRefused(LaunchRefusal.PROMPT_UNMEASURED)
        if envelope_path is not None:
            object.__setattr__(handle, "envelope_path", Path(envelope_path))
        self._states[handle.correlation_token] = PollResult(PollState.RUNNING)
        return handle

    def complete(
        self, token: str, exit_code: int = 0, detail: str = "ENVELOPE_SUCCESS"
    ) -> None:
        self._states[token] = PollResult(PollState.EXITED, exit_code, detail)

    def set_agent_status(self, token: str, status: Optional[str]) -> None:
        self._statuses[token] = status

    def agent_status(self, handle: LaunchHandle) -> Optional[str]:
        return self._statuses.get(handle.correlation_token)

    def wait_for_idle(self, handle: LaunchHandle, timeout_s: float = 60.0) -> None:
        del timeout_s
        if self.agent_status(handle) != "idle":
            raise RuntimeError(
                "AGENT_INTERACTIVE_READY_TIMEOUT:{}".format(handle.agent_name)
            )

    def poll(self, handle: LaunchHandle) -> PollResult:
        return self._states.get(
            handle.correlation_token, PollResult(PollState.GONE, detail="AGENT_GONE")
        )

    def cancel(self, handle: LaunchHandle, deadline: float) -> None:
        del deadline
        self._states[handle.correlation_token] = PollResult(
            PollState.GONE, detail="CANCELLED"
        )

    def retain(self, handle: LaunchHandle) -> None:
        if self._handles.get(handle.correlation_token) is not handle:
            raise LaunchRefused(
                LaunchRefusal.BINDING_MISMATCH, handle.correlation_token
            )
        self.retained.append(handle.correlation_token)

    def complete_run(
        self,
        handles: Sequence[LaunchHandle],
        *,
        project_identity: str = "",
        timeout_s: float = 60.0,
    ) -> None:
        del project_identity, timeout_s
        tokens = tuple(handle.correlation_token for handle in handles)
        if tokens in self.completed:
            return
        for handle in handles:
            if self._handles.get(handle.correlation_token) is not handle:
                raise LaunchRefused(
                    LaunchRefusal.BINDING_MISMATCH, handle.correlation_token
                )
            if self.agent_status(handle) not in (None, "idle"):
                raise LaunchRefused(
                    LaunchRefusal.SESSION_RENAME_UNCONFIRMED,
                    handle.correlation_token,
                    pane_created=True,
                )
            self._states[handle.correlation_token] = PollResult(
                PollState.GONE, detail="RUN_COMPLETE"
            )
        self.completed.append(tokens)

    def classify(self, exc: BaseException) -> ErrorClass:
        return classify_error(exc)

    def provision(self, worktree: Path) -> None:
        return None



#: Herdr `error.code`s naming a condition another attempt can survive.
#:
#: The code is a typed field on `HerdrCallError`, parsed from herdr's
#: `{"error": {"code": ...}}` envelope by `herdr_error_code` -- never matched
#: out of the message, which §1.2 forbids and which herdr may reword at any
#: release. `agent_pane_busy` is the one observed member: herdr refuses to
#: start an agent in a pane that is not an available shell, and the next
#: attempt gets a pane of its own. Without it the refusal fell through to
#: `EXECUTION` -> `STARTUP` and was budgeted as a broken launcher rather than
#: as contention.
TRANSIENT_HERDR_ERROR_CODES: frozenset = frozenset({"agent_pane_busy"})


def workspace_of(pane_id: str) -> str:
    """The workspace half of a herdr pane id (`w13A:p29` -> `w13A`).

    Herdr's pane id is a structured identifier, not prose: the workspace and
    the pane are two fields joined by a colon, and reading the first is
    reading a field rather than matching a message (§1.2). An id without a
    colon has no workspace to report and yields `""`, which callers treat as
    "unknown" rather than as a match.
    """
    workspace, sep, _pane = str(pane_id).partition(":")
    return workspace if sep else ""


def _herdr_error_code_of(exc: BaseException) -> str:
    """Herdr's own error code for a failure, following the `from` chain.

    A refusal is restated as a `LaunchRefused` before it leaves `launch`, so
    the `HerdrCallError` that carries the code arrives as `__cause__`. Walking
    the chain reads the same typed field wherever the restatement happened.
    """
    seen = set()
    cursor: Optional[BaseException] = exc
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, HerdrCallError) and cursor.code:
            return cursor.code
        cursor = cursor.__cause__
    return ""


def classify_error(exc: BaseException) -> ErrorClass:
    if _herdr_error_code_of(exc) in TRANSIENT_HERDR_ERROR_CODES:
        return ErrorClass.TRANSIENT
    cursor: Optional[BaseException] = exc
    seen = set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        if isinstance(cursor, PromptSubmissionUnobservable):
            # An unreadable revision counter is a missing observation about
            # herdr, not a wedged composer (D9).
            return ErrorClass.TRANSIENT
        cursor = cursor.__cause__
    if isinstance(exc, FileNotFoundError):
        return ErrorClass.CONFIGURATION
    if isinstance(exc, PermissionError):
        return ErrorClass.AUTHENTICATION
    if isinstance(exc, TimeoutError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return ErrorClass.PROTOCOL
    return ErrorClass.EXECUTION
