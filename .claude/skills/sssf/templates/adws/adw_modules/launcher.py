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
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

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


class HandleOwnershipError(RuntimeError):
    """A live Herdr actor could not be proven to own the persisted handle."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(
            "{}{}".format(self.code, ":" + self.detail if self.detail else "")
        )


class HandleAbsent(HandleOwnershipError):
    """Herdr proved the persisted pane or actor id no longer exists."""


class HandleAdoptionRefused(HandleOwnershipError):
    """Herdr answered, but its typed identity disagreed with the ledger."""


class _WorkspaceGone(RuntimeError):
    """The run's memoized workspace id no longer names a live workspace.

    Internal to the launcher and never raised past it: `_tab_for` either
    recovers by re-resolving, or restates this as a typed `LaunchRefused`. It
    exists so the recovery path is selected by a raised type rather than by
    re-parsing an error code at the call site (#79).
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
        raise LaunchRefused(LaunchRefusal.SCRATCH_REDIRECT_MISSING, ",".join(missing))
    flags: List[str] = []
    for key in SCRATCH_ENV_KEYS:
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
    #: Durable lane identity used only as the tab-adoption key. A derived
    #: reviewer receives its build lane's key rather than its derived node id.
    lane_key: str = ""
    #: Authored lane name shown on the Herdr tab and actor pane labels.
    lane_label: str = ""
    #: This actor's role in the lane: `builder`, `reviewer`, or `tester`.
    pane_role: str = ""
    #: Actor attempt/session generation shown as `aN`. Standalone author and
    #: deliver panes leave it unset and retain their role-only label.
    attempt_no: Optional[int] = None
    #: How many agent panes the caller expects this tab to hold. The tab's
    #: column count is computed from it and then only ever grows, so a
    #: declared size is what makes the grid exact: a node declaring 2 gets one
    #: row of two, one declaring 4 gets 2x2. Left at 0 the launcher learns the
    #: count as panes arrive, which is correct for 1, 2 and 3 panes and merely
    #: wider than the table for 4 and 5.
    pane_group_size: int = 0
    #: Secondary escape hatch, default off. The operator's tool policy is the
    #: omp profile (`--profile`). True appends
    #: `permissions.route_capability_argv`; False passes no `--tools` /
    #: `--disallowedTools`. Maestro does not editorialise about tools unless
    #: asked.
    restrict_tools: bool = False


#: Direct Claude sessions must delegate substantial work rather than silently
#: duplicating a spawned subagent's task in the parent, and must judge a
#: teammate idle only on positive evidence. This is part of every Claude
#: prompt, at the universal launch chokepoint rather than at individual
#: scheduler call sites. The idle-evidence sentence is the agent-side twin of
#: the runtime-side law: LIVE_WORKING_STATUSES (watchdog.py /
#: finalization_window.py) excludes "idle" for the same reason -- silence,
#: flat mtime, and idle_notification are heartbeats, not completion.
#: The pane status that means "back at the composer, not doing anything" --
#: and equally "between a tool result and the next message" and "blocked
#: inside a tool call", which is why one sample of it decides nothing.
#:
#: Stated here rather than imported from `finalization_window`, which names
#: the same string for the reviewer. That module reaches this one through
#: `watchdog -> scheduler_types -> worktree`, so the dependency runs one way
#: only. `test_step7_launcher` asserts the two stay equal, which is what keeps
#: a restatement from becoming a divergence.
AGENT_QUIESCENT_STATUS = "idle"

#: How long `AGENT_QUIESCENT_STATUS` must hold, with no transcript record
#: appearing, before `poll` reads it as a turn that stopped without declaring.
#: B14's rule and the reviewer window's own number
#: (`finalization_window.DEFAULT_QUIESCENCE_CONFIRM_S`), bound to it by the
#: same test: two numbers for one clock is how a raised default comes to look
#: like it did nothing.
AGENT_QUIESCENCE_CONFIRM_S = 60.0


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
    #: Durable Herdr placement captured from the pane's resolved tab layout.
    #: Empty only for non-run launchers that intentionally have no workspace/tab.
    workspace_id: str = ""
    tab_id: str = ""
    lane_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )


@dataclass(frozen=True)
class PersistedActorHandle:
    """The durable identity required to reclaim one interactive actor.

    Labels and prompt bytes are intentionally absent: both are display/content
    and neither establishes that this process owns the pane.  Resume names the
    Herdr pane, workspace, tab, and agent ids recorded at launch, then verifies
    that exact placement and the worktree binding before restoring an in-memory
    ``LaunchHandle``.
    """

    correlation_token: str
    pane_id: str
    agent_name: str
    launched_cwd: Path
    transcript_path: Optional[Path] = None
    envelope_path: Optional[Path] = None
    environment: Mapping[str, str] = field(default_factory=dict)
    workspace_id: str = ""
    tab_id: str = ""
    lane_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "launched_cwd", Path(self.launched_cwd))
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
    def reclaim(self, token: str) -> Tuple[LaunchHandle, ...]: ...
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
    ) -> LaunchHandle: ...
    def adopt(self, persisted: PersistedActorHandle) -> LaunchHandle: ...
    def retire_for_replacement(
        self, persisted: PersistedActorHandle, deadline: float
    ) -> None: ...
    def close_actorless_pane(self, persisted: PersistedActorHandle) -> None: ...
    def wait_for_idle(self, handle: LaunchHandle, timeout_s: float = 60.0) -> None: ...


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
    """Build omp's argv without the node prompt.

    OMP must first reach its interactive composer. Passing `@<prompt-path>` as
    a startup positional races that readiness boundary: the process can consume
    only the prompt's first command before the full node instruction is
    available. `launch` therefore starts an empty interactive session, waits
    for readiness, and submits the complete prompt atomically through Herdr.

    Tool policy is the configured `--profile`. `--tools` is a secondary hatch,
    off unless `spec.restrict_tools` is set.
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
    if spec.restrict_tools:
        argv.extend(permissions.route_capability_argv(spec.route))
    if spec.session_dir.is_dir() and any(spec.session_dir.glob("*.jsonl")):
        argv.append("-c")
    return tuple(argv)


def build_claude_argv(binary: Path, spec: LaunchSpec) -> Tuple[str, ...]:
    """Claude's argv. Tool denial is the same hatch the omp route carries.

    Expressed as `--disallowedTools` rather than as `--tools` when the hatch
    is on: see `permissions.route_capability_argv`. Default is no denial
    list. Both flags exist in the installed binary (`claude` 2.1.237 reports
    `--disallowedTools, --disallowed-tools <tools...>`), which is `--help`
    capture — the same evidence level §9.6 already records for
    `--dangerously-skip-permissions` and `--remote-control` beside it.
    """
    denial = (
        permissions.route_capability_argv(spec.route) if spec.restrict_tools else ()
    )
    return (
        str(binary),
        "--model",
        spec.model,
        "--effort",
        spec.effort,
        "--dangerously-skip-permissions",
        "--remote-control",
        *denial,
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

    §7.6 names three liveness signals and §16.3 item 17 records that one of
    them, PROCESS_DEAD, cannot convict an agent attempt: `attempt.pid` was
    only ever `handle.process_group`, which is absent for a herdr-spawned
    agent, so `watchdog.py`'s `attempt.pid is not None` branch never ran and
    TURN_TIMEOUT and NODE_TIMEOUT carried the whole burden alone (#20).

    `herdr pane process-info` does report a group — the same payload
    `_available_shell` already reads for its `shell_pid` / `pgid` /
    `foreground_processes` triple. What it reports is the pane's *foreground*
    group, which is the agent while the agent is running and the pane's own
    shell when nothing is.

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


class PromptNotSubmitted(RuntimeError):
    """The composer holds the prompt text and will not submit it.

    Raised only after every recovery attempt has been spent *and* the pane's
    revision counter was legible throughout, so it means the pane is genuinely
    wedged rather than merely slow or merely unreadable.
    """


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
    """


#: How many times to press Enter on an unsubmitted composer before giving up.
#: More than one because a single fallback is what failed in the recorded
#: incident: the Enter went in while the composer was still not accepting
#: input, the one verification wait then blocked for the whole remaining
#: budget, and the 600s window expired around a prompt that was never sent.
SUBMIT_ATTEMPTS = 4


def pane_revision(herdr_call: Callable[..., dict], pane_id: str) -> Optional[int]:
    """The pane's monotonic revision counter, or None when it cannot be read.

    Diagnostic only. §1.2 permits reading this typed integer, but it cannot
    prove a prompt was submitted: pasting the text is itself a repaint, so the
    counter advances whether the composer accepted the turn or is still holding
    it. `agent_status` cannot either — a pane that never accepted the prompt
    and a pane whose short turn already finished both report `idle`.
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
    working_proves: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    submission_recorded: Optional[Callable[[], bool]] = None,
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
    # Diagnostic meter only. A revision advance does not prove consumption:
    # paste itself repaints. Readings distinguish Unobservable from
    # NotSubmitted after recovery is spent. `working` is not proof here either.
    baseline = pane_revision(herdr_call, pane_id)
    # Every legible reading taken *after* the prompt was offered. Emptiness is
    # the structural fact D9 turns on: it says the meter was never readable,
    # which is not the same claim as "the meter did not move".
    readings: List[int] = []
    # Recovery must not wait on `idle`: the unsubmitted composer is already
    # idle, so Herdr would return immediately and all Enter attempts would be
    # spent back-to-back before the composer had another chance to become
    # interactive. A fast accepted turn is proven by a rising transcript
    # record, never by this wait returning.
    recovery_until = tuple(status for status in until if status != "idle")
    if not recovery_until:
        recovery_until = ("working",)
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
        current = pane_revision(herdr_call, pane_id)
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
                return bool(submission_recorded())
            except Exception:
                return False
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
            except Exception:
                return False
            agent = _extract(payload, "agent")
            if isinstance(agent, dict) and (
                agent.get("agent_status") or agent.get("status")
            ) == "working":
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
        try:
            herdr_call(*argv, timeout=budget_s + 5.0)
        except Exception:
            pass
        return consumed()

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
    except Exception:
        # A refused `esc` is not a failed submission. Fall through to the
        # Enter, which is the thing that actually decides.
        pass
    sleep(PASTE_SETTLE_S)
    try:
        herdr_call("pane", "send-keys", pane_id, "enter", timeout=30.0)
    except Exception:
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
        except Exception:
            # A send-keys that fails on one round is not fatal: the pane may be
            # mid-repaint. Fall through to the verify, which is the thing that
            # actually decides.
            pass
        if consumed():
            return
        if round_no + 1 < rounds:
            sleep(0.5)
    if submission_recorded is not None:
        # The agent runtime writes the transcript record as the turn starts,
        # which can trail the last Enter by a moment. Give it a bounded grace
        # rather than reaping a pane that has in fact begun work.
        grace_deadline = time.monotonic() + TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S
        while True:
            if consumed():
                return
            if time.monotonic() >= grace_deadline:
                break
            sleep(0.1)
        raise PromptNotSubmitted(
            "AGENT_PROMPT_UNSUBMITTED:{0} after {1} submit attempts".format(
                target, rounds
            )
        )
    if baseline is None or not readings:
        # Never a legible before/after pair, so there is no fact about the
        # prompt here at all -- only a fact about herdr. Transient by
        # construction, and the pane is left for the caller to reap exactly as
        # the wedged case is.
        raise PromptSubmissionUnobservable(
            "AGENT_PROMPT_UNOBSERVED:{0} after {1} submit attempts".format(
                target, rounds
            )
        )
    raise PromptNotSubmitted(
        "AGENT_PROMPT_UNSUBMITTED:{0} after {1} submit attempts".format(target, rounds)
    )


TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S = 10.0


def prompt_submission_marks(
    handle: LaunchHandle, prompt_path: Path, *, chunk_size: int = 64 * 1024
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
    except OSError:
        return total


def prompt_submission_recorded(
    handle: LaunchHandle, prompt_path: Path, *, chunk_size: int = 64 * 1024
) -> bool:
    """Whether the actor transcript durably names this exact prompt submission.

    This is the authoritative proof of submission.  Herdr's revision advance
    cannot serve as one -- the paste itself repaints the pane, so the counter
    moves identically for a submitted turn and for a composer still holding the
    text -- and any live signal would in any case disappear if the scheduler
    died before its next SQLite write.  The transcript's user-message record
    is written by the agent runtime only when a turn actually starts, and it
    survives that gap.  Match the absolute
    ``@<path>`` bootstrap rather than pane text or a candidate label, and scan
    in bounded chunks so a long-lived reviewer session is not copied into
    memory merely to recover its latest dispatch.
    """
    transcript = handle.transcript_path
    if transcript is None:
        return False
    marker = ("@" + str(Path(prompt_path).resolve())).encode("utf-8")
    if chunk_size < len(marker):
        chunk_size = len(marker)
    overlap = b""
    try:
        with Path(transcript).open("rb") as source:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    return False
                window = overlap + chunk
                if marker in window:
                    return True
                overlap = window[-(len(marker) - 1) :] if len(marker) > 1 else b""
    except OSError:
        return False


def _rising_submission_record(
    handle: LaunchHandle, prompt_path: Path
) -> Callable[[], bool]:
    """A predicate that turns true when THIS offer is recorded.

    Snapshots the transcript's existing marker count now, so a reused prompt
    path from a previous turn on the same actor cannot stand in as proof.
    Missing transcript cannot prove a turn: the predicate stays false rather
    than handing the caller a live-signal fallback. Paste-repaint moves the
    pane revision whether the composer submitted or not (2026-08-27).
    """
    if handle.transcript_path is None:
        return lambda: False
    before = prompt_submission_marks(handle, prompt_path)
    return lambda: prompt_submission_marks(handle, prompt_path) > before


def wait_for_prompt_submission_record(
    handle: LaunchHandle,
    prompt_path: Path,
    timeout_s: float = TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait boundedly for the durable transcript half of submission proof."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if prompt_submission_recorded(handle, prompt_path):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sleep(min(0.1, remaining))


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
        agent = _extract(payload, "agent")
        if not isinstance(agent, dict):
            result = payload.get("result") if isinstance(payload, dict) else None
            agent = result if isinstance(result, dict) else {}
        status = agent.get("agent_status") or agent.get("status")
        return status in ("idle", "done")

    def ready() -> bool:
        try:
            payload = herdr_call("agent", "get", name)
        except RuntimeError:
            return False
        return settled(payload)

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
        raise RuntimeError("AGENT_INTERACTIVE_READY_TIMEOUT:{}".format(name)) from exc
    if settled(outcome) or ready():
        return
    raise RuntimeError("AGENT_INTERACTIVE_READY_TIMEOUT:{}".format(name))


#: How long `launch` waits for Herdr to report the agent's transcript.
#: Bounded at the prompt submission's own 60s. This runs only after the coder
#: is ready and has been given its prompt, so a JSONL path or Claude session ID
#: that cannot resolve within a minute is absent rather than merely early.
TRANSCRIPT_PATH_TIMEOUT_S = 60.0


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
            _extract(payload, "agent"), launched_cwd, environment
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
    """One tab's grid: the panes it holds, in slot order, and its width.

    Not a dataclass with a lock bolted on afterwards but a small mutable
    record that owns its own lock, because the reservation and the split have
    to be one atomic step. Two launches into the same tab must take two
    distinct slots *and* see the parent of the later slot already created --
    reserving under a lock and splitting outside it would let pane 2 name a
    pane 1 that does not exist yet. Splits are one CLI call, so holding the
    lock across it serialises one tab and nothing else; two tabs still open
    their panes concurrently.
    """

    __slots__ = ("tab_id", "panes", "claimed", "cols", "lock")

    def __init__(self, tab_id: str, panes: List[Optional[str]], claimed: int) -> None:
        self.tab_id = tab_id
        #: Grid slot -> pane id, positional. A reaped pane leaves `None`
        #: behind rather than shifting its neighbours, because the slots after
        #: it name real panes whose geometry did not change.
        self.panes: List[Optional[str]] = list(panes)
        #: How many slots have been handed to an agent. Starts at 0 for a tab
        #: this launcher created -- its root pane is an empty shell the first
        #: agent takes over -- and at 1 for the legacy seed, which is the
        #: *caller's own* pane and must never be handed to anything.
        self.claimed = claimed
        self.cols = 1
        self.lock = threading.Lock()

    def nearest_live(self, index: int) -> Optional[str]:
        """The nearest surviving pane to `index`, searching back then forward.

        The grid's chosen parent may have been reaped since it was created --
        a lane's earlier pane may be closed before its later actor arrives.
        Falling back to the nearest live pane keeps the split inside this
        lane's tab, which is the guarantee that matters, and costs only the
        exactness of one cell. Backwards first
        because an earlier slot is the parent the grid would have chosen next;
        forwards after, because refusing a launch while a live pane sits one
        slot later would be a refusal about bookkeeping rather than about
        herdr.
        """
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
        #: The label of the herdr workspace this launcher creates for its run,
        #: named for the plan it is running. Naming it is what makes the
        #: workspace *this run's*: every pane the run opens lands in it, and no
        #: pane of an unrelated run does. Empty keeps the pre-workspace
        #: behaviour -- panes split from the caller's own once-resolved pane,
        #: with no tabs -- which is what the standalone reviewer and plan
        #: author verbs get, since neither of those is a run.
        self.workspace_label = str(workspace_label or "")
        #: How long `agent start` re-offers a pane herdr calls busy. An
        #: attribute rather than a constant read directly so a test can drive
        #: the refusal path without spending the production window; nothing in
        #: the runtime sets it.
        self.agent_start_busy_window_s = AGENT_START_BUSY_WINDOW_S
        #: How long `idle` must hold, with no transcript record appearing,
        #: before `poll` convicts a builder turn of ending without an
        #: envelope. An attribute for the same reason
        #: `agent_start_busy_window_s` is one: a test drives the branch
        #: without spending the production window. Nothing in the runtime
        #: sets it.
        self.quiescence_confirm_s = AGENT_QUIESCENCE_CONFIRM_S
        self._handles_lock = threading.RLock()
        self._handles: Dict[str, LaunchHandle] = {}
        self._tailers: Dict[str, TranscriptTailer] = {}
        #: token -> (monotonic stamp the current `idle` run began, transcript
        #: record count at that stamp). Guarded by `_handles_lock`.
        self._quiescent_since: Dict[str, Tuple[float, int]] = {}
        self._proven_absent: Dict[str, LaunchHandle] = {}
        #: The pane every split is taken from, resolved once from herdr's
        #: `--current` selector and then never re-read. `None` until the first
        #: launch asks. A failure to resolve is **not** recorded here: it is a
        #: typed refusal (`SPLIT_PARENT_UNRESOLVED`) and the next launch
        #: re-asks, because the alternative — falling back to the selector —
        #: is the mutable read this field exists to remove.
        self._split_parent_id: Optional[str] = None
        #: The workspace every pane of this run belongs to, taken from the
        #: split parent's own id the moment it is resolved. A split of a fixed
        #: parent lands beside that parent, so a child reporting a different
        #: workspace is a split that escaped the run's workspace and is
        #: refused rather than adopted.
        self._workspace_id: str = ""
        #: `pane_group` -> the tab that group's panes live in. One entry per
        #: node, created on that node's first launch and reused by every later
        #: agent of the same node, so a builder and its reviewer share a
        #: rectangle. Guarded by `_handles_lock`; each tab's own lock guards
        #: the slots inside it.
        self._tabs: Dict[str, _TabLayout] = {}
        #: The tab `workspace create` opens along with the workspace. It is an
        #: empty shell nobody asked for, so it is closed once a real tab
        #: exists to keep it from being the workspace's last tab -- a close
        #: herdr refuses, correctly (§3.5 F2).
        self._seed_tab_id: str = ""

    @property
    def workspace_id(self) -> str:
        """The immutable Herdr workspace bound to this run launcher."""
        with self._handles_lock:
            return self._workspace_id

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
            raise RuntimeError("LAUNCH_REFUSED:{}".format(exc)) from exc
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

    def _run_workspace(self, environment: Mapping[str, str]) -> str:
        """The herdr workspace every pane of this run lands in, created once.

        Creating a workspace rather than adopting one is the fix for the
        complaint that a run's panes land wherever focus happens to be: a
        workspace this launcher made is one no other run holds, so "the run's
        panes" and "the panes in this workspace" are the same set and the
        operator has one place to look. It is also strictly more determinate
        than the selector read it replaces -- there is no ambient state to
        read at all.

        Created under `_handles_lock` so concurrent first launches make one
        workspace between them, exactly as `_split_parent` resolves one
        parent. A refusal is typed and **not** cached: the next launch
        genuinely re-asks herdr.
        """
        with self._handles_lock:
            if self._workspace_id:
                return self._workspace_id
            try:
                payload = self._herdr(
                    "workspace",
                    "create",
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
            self._workspace_id = str(workspace_id)
            seed = _extract(payload, "tab")
            if isinstance(seed, dict) and seed.get("tab_id"):
                self._seed_tab_id = str(seed["tab_id"])
            return self._workspace_id

    def _close_seed_tab(self, environment: Mapping[str, str]) -> None:
        """Drop the shell tab `workspace create` opens, once a real one exists.

        Best-effort and display-only: a workspace carrying one extra empty tab
        is untidy, not incorrect, so a refused close is dropped rather than
        turned into a launch refusal. Called only after another tab exists,
        because herdr refuses to close a workspace's last tab and is right to.
        """
        tab_id, self._seed_tab_id = self._seed_tab_id, ""
        if not tab_id:
            return
        try:
            self._herdr("tab", "close", tab_id, env=environment)
        except BaseException:
            return

    def _tab_create(
        self,
        workspace_id: str,
        label: str,
        worktree: Path,
        env_flags: Sequence[str],
        environment: Mapping[str, str],
    ) -> dict:
        """One `tab create`, raising `_WorkspaceGone` when the id is dead.

        Keyed on herdr's typed `error.code`, never on the message text: §1.2
        forbids classifying on prose, and `herdr_error_code` returns `""`
        rather than a guess for anything it does not recognise, so an
        unrecognised refusal cannot be mistaken for this one.

        Clearing the cache is done here, at the point the answer arrives,
        rather than by the caller — a caller that forgot would re-ask with the
        same dead id, which is the defect this exists to close.
        """
        try:
            return self._herdr(
                "tab",
                "create",
                "--workspace",
                workspace_id,
                "--label",
                label,
                "--cwd",
                str(worktree),
                "--no-focus",
                *env_flags,
                env=environment,
            )
        except HerdrCallError as exc:
            if _herdr_error_code_of(exc) != "workspace_not_found":
                raise
            self._invalidate_workspace_layout(workspace_id)
            raise _WorkspaceGone(str(exc)) from exc

    def _invalidate_workspace_layout(self, workspace_id: str) -> None:
        """Release placement state whose workspace Herdr has proved absent."""
        with self._handles_lock:
            if self._workspace_id != workspace_id:
                return
            self._workspace_id = ""
            self._seed_tab_id = ""
            self._tabs.clear()

    def _tab_for(
        self,
        spec: LaunchSpec,
        worktree: Path,
        env_flags: Sequence[str],
        environment: Mapping[str, str],
    ) -> _TabLayout:
        """The tab this lane's panes live in, created on its first launch.

        One tab per lane, labelled with the authored build-lane name, is what
        S2 buys: the sidebar becomes an index of the lanes in flight instead
        of a flat list of panes identifiable only by working directory. Its
        tester, builder, and reviewer are neighbours rather than strangers.

        The tab is created with the launch's `--cwd` and `--env`, so its root
        pane is a usable shell for the first agent rather than a spare cell.

        Held under `_handles_lock` across the herdr call for the same reason
        `_split_parent` is: two concurrent launches of one node must produce
        one tab, and a tab created twice is two rectangles for one node.
        """
        group = spec.lane_key or ""
        with self._handles_lock:
            requested_workspace = str(spec.workspace_label or "")
            if (
                self.workspace_label
                and requested_workspace
                and requested_workspace != self.workspace_label
            ):
                raise LaunchRefused(
                    LaunchRefusal.WORKSPACE_DRIFT,
                    "workspace label {} != {}".format(
                        requested_workspace, self.workspace_label
                    ),
                    pane_created=False,
                )
            if requested_workspace and not self.workspace_label:
                self.workspace_label = requested_workspace
            existing = self._tabs.get(group)
            if existing is not None and not existing.empty():
                return existing
            if not self.workspace_label:
                # Pre-workspace placement, kept for the verbs that are not a
                # run: every pane splits from the caller's own once-resolved
                # pane. That pane belongs to whoever called, so it is claimed
                # from the start and never handed to an agent.
                seed = self._split_parent(environment)
                layout = _TabLayout(tab_id="", panes=[seed], claimed=1)
                self._tabs[group] = layout
                return layout
            workspace_id = self._run_workspace(environment)
            label = str(spec.lane_label or group or self.workspace_label)
            try:
                payload = self._tab_create(
                    workspace_id, label, worktree, env_flags, environment
                )
            except _WorkspaceGone:
                # The cached workspace no longer exists. Re-resolve once and
                # ask again: `_run_workspace` creates a fresh one, so the
                # retry is a different question rather than the same dead one
                # (#79). The cache is cleared inside `_tab_create`, under this
                # same reentrant lock, so a concurrent launch of another node
                # resolves the new workspace rather than racing to make a
                # third.
                workspace_id = self._run_workspace(environment)
                try:
                    payload = self._tab_create(
                        workspace_id, label, worktree, env_flags, environment
                    )
                except _WorkspaceGone as exc:
                    # Twice in a row is not a stale cache. Something is
                    # destroying workspaces as fast as this makes them, and a
                    # third attempt re-asks a question that has now been
                    # answered the same way twice.
                    raise LaunchRefused(
                        LaunchRefusal.TAB_UNRESOLVED,
                        "workspace vanished twice: {0}".format(exc),
                    ) from exc
                except BaseException as exc:
                    raise LaunchRefused(
                        LaunchRefusal.TAB_UNRESOLVED,
                        "{0}: {1}".format(type(exc).__name__, exc),
                    ) from exc
            except BaseException as exc:
                raise LaunchRefused(
                    LaunchRefusal.TAB_UNRESOLVED,
                    "{0}: {1}".format(type(exc).__name__, exc),
                ) from exc
            tab = _extract(payload, "tab")
            root = _extract(payload, "root_pane")
            tab_id = tab.get("tab_id") if isinstance(tab, dict) else None
            root_id = root.get("pane_id") if isinstance(root, dict) else None
            if not tab_id or not root_id:
                raise LaunchRefused(LaunchRefusal.TAB_UNRESOLVED, "NO_TAB")
            layout = _TabLayout(tab_id=str(tab_id), panes=[str(root_id)], claimed=0)
            self._tabs[group] = layout
            self._close_seed_tab(environment)
            return layout

    def _acquire_pane(
        self,
        spec: LaunchSpec,
        worktree: Path,
        env_flags: Sequence[str],
        environment: Mapping[str, str],
    ) -> Tuple[str, _TabLayout]:
        """One pane for this launch, in this lane's tab, at its grid slot.

        The slot is reserved and the split taken under the tab's own lock, so
        launch *k* into a tab always finds slot *k-1* already created. The
        first agent of a tab takes the tab's root pane and splits nothing;
        every later one splits the pane `split_plan` names.

        Column count is decided here and only ever grows: the caller's
        declared `pane_group_size` if it named one, otherwise the count
        observed so far. Frozen-then-growing rather than recomputed, because a
        column count that shrank would ask for a grid the existing panes are
        not in.
        """
        for recovery in range(2):
            layout = self._tab_for(spec, worktree, env_flags, environment)
            with layout.lock:
                index = layout.claimed
                layout.claimed += 1
                declared = max(int(spec.pane_group_size or 0), index + 1)
                layout.cols = max(layout.cols, grid_for(declared)[1])
                if index < len(layout.panes) and layout.panes[index]:
                    # The tab's own root pane, already opened with this launch's
                    # working directory and redirection. Nothing to split.
                    return str(layout.panes[index]), layout
                parent_index, direction = split_plan(index, layout.cols)
                parent_id = layout.nearest_live(parent_index)
                if parent_id is None:
                    raise LaunchRefused(LaunchRefusal.TAB_UNRESOLVED, "NO_LIVE_PARENT")
                try:
                    split = self._herdr(
                        "pane",
                        "split",
                        parent_id,
                        "--direction",
                        direction,
                        "--cwd",
                        str(worktree),
                        "--no-focus",
                        *env_flags,
                        env=environment,
                    )
                except HerdrCallError as exc:
                    if _herdr_error_code_of(exc) != "workspace_not_found":
                        raise
                    vanished_workspace = workspace_of(parent_id)
                else:
                    pane = _extract(split, "pane")
                    if not isinstance(pane, dict) or not pane.get("pane_id"):
                        # No id means nothing to close: herdr may hold a pane it did
                        # not report, and an unreapable pane is exactly the case
                        # `pane_created` exists to keep honest.
                        raise LaunchRefused(LaunchRefusal.NO_PANE, pane_created=True)
                    pane_id = str(pane["pane_id"])
                    while len(layout.panes) <= index:
                        layout.panes.append(None)
                    layout.panes[index] = pane_id
                    return pane_id, layout
            # `workspace_not_found` is proof that every cached tab and slot in
            # this workspace is stale. Drop them together before re-resolving:
            # keeping the claimed split slot would make the fresh tab start at
            # its second cell, and keeping the old layout would re-split its
            # dead root on the next launch.
            self._invalidate_workspace_layout(vanished_workspace)
            if recovery:
                raise LaunchRefused(
                    LaunchRefusal.TAB_UNRESOLVED,
                    "workspace vanished twice while splitting",
                )
        raise AssertionError("UNREACHABLE")

    def _label_pane(
        self, pane_id: str, spec: LaunchSpec, environment: Mapping[str, str]
    ) -> None:
        """Name the pane after its role and generation, once, at launch.

        The lane is already the tab label. Repeating it on every pane hides the
        part an operator needs to distinguish inside that tab: tester, builder,
        or reviewer, and the actor generation currently occupying the pane.

        `label` is herdr's own durable per-pane field and survives the agent
        taking over the terminal -- verified against the real binary: a pane
        renamed and then given a `claude` agent still reports its label while
        `terminal_title` reads `Claude Code`.

        Display-only, so a refused rename is dropped rather than spent as a
        launch refusal: a run that stops because a pane could not be named has
        traded the work for the caption.
        """
        role = str(spec.pane_role or "")
        attempt = "a{}".format(spec.attempt_no) if spec.attempt_no is not None else ""
        label = "-".join(part for part in (role, attempt) if part)
        if not label:
            return
        try:
            self._herdr("pane", "rename", pane_id, label, env=environment)
        except BaseException:
            return

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
            if exc.code not in ("pane_not_found", "workspace_not_found"):
                return False
            if exc.code == "workspace_not_found":
                self._invalidate_workspace_layout(workspace_of(pane_id))
            self._forget_pane(pane_id)
            return True
        except BaseException:
            return False
        self._forget_pane(pane_id)
        return True

    def launch(self, spec: LaunchSpec) -> LaunchHandle:
        if not self.admitted_routes.admits(spec.route):
            raise LaunchRefused(LaunchRefusal.ROUTE_NOT_ADMITTED, spec.route)
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
        env_flags = pane_env_flags(environment)
        # Placement, in three steps that are each a property of the run rather
        # than of whatever holds focus: the run's own workspace, this node's
        # own tab inside it, and this agent's own grid slot inside that tab.
        pane_id, layout = self._acquire_pane(spec, worktree, env_flags, environment)
        with layout.lock:
            pane_is_in_layout = pane_id in layout.panes
            placement_tab_id = layout.tab_id
        if not pane_is_in_layout:
            closed = self._reap_pane(pane_id, environment)
            raise LaunchRefused(
                LaunchRefusal.TAB_UNRESOLVED,
                "ACQUIRED_PANE_NOT_IN_LAYOUT",
                pane_created=not closed,
            )
        placement_workspace_id = (
            workspace_of(placement_tab_id)
            or self._workspace_id
            or workspace_of(pane_id)
        )
        # Every pane of one run belongs to one workspace. A split of a fixed
        # parent lands beside that parent, so a child reporting a different
        # workspace means the placement escaped — the shape that scattered one
        # run's agents across w13F, w13G, w13H, w13J and w13K while its first
        # pane sat in w13A, and an operator watching w13A then sees a factory
        # that has stopped. Checked rather than assumed, because the guarantee
        # is worth nothing if nothing measures it.
        landed = workspace_of(pane_id)
        if self._workspace_id and landed and landed != self._workspace_id:
            closed = self._reap_pane(pane_id, environment)
            raise LaunchRefused(
                LaunchRefusal.WORKSPACE_DRIFT,
                "{0}!={1}".format(landed, self._workspace_id),
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
            # Reap first, then state what the reap achieved. Re-raising
            # herdr's own `HerdrCallError` from here was the 2026-08-18
            # defect: it is not a `LaunchRefused`, so `LaunchFailed`'s
            # fail-closed `pane_created` said a pane survived a close that had
            # just succeeded, the scheduler quiesced an attempt whose handle
            # was never registered, and PROCESS_GROUP_UNTRACKED replaced a
            # retryable launch failure with a terminal QUIESCENCE_UNPROVEN.
            closed = self._reap_pane(pane_id, environment)
            if not isinstance(exc, Exception):
                # KeyboardInterrupt/SystemExit are not launch outcomes.
                raise
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
        if actual != worktree:
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
        agent = _extract(started, "agent")
        transcript = _agent_transcript_path(agent, worktree, environment)
        handle = LaunchHandle(
            spec.correlation_token,
            pane_id,
            name,
            worktree,
            transcript_path=transcript,
            envelope_path=spec.envelope_path,
            environment=environment,
            workspace_id=placement_workspace_id,
            tab_id=placement_tab_id,
            lane_key=spec.lane_key,
        )
        with self._handles_lock:
            self._handles[spec.correlation_token] = handle
            self._proven_absent.pop(spec.correlation_token, None)
            if transcript:
                self._tailers[spec.correlation_token] = TranscriptTailer(transcript)
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

            # The rising transcript record is the only positive proof. Obtain
            # it before offering so `_rising_submission_record` is a real
            # predicate, not None. Submitting first left the 2026-08-27 grok
            # builder at revision 1 with `@prompt` unsubmitted while
            # `consumed()` returned True on the meter fallback.
            if transcript is None:
                transcript = wait_for_agent_transcript(
                    lambda *args, **kwargs: self._herdr(
                        *args, env=environment, **kwargs
                    ),
                    name,
                    TRANSCRIPT_PATH_TIMEOUT_S,
                    launched_cwd=worktree,
                    environment=environment,
                )
                if transcript is not None:
                    object.__setattr__(handle, "transcript_path", transcript)
                    with self._handles_lock:
                        self._tailers[spec.correlation_token] = TranscriptTailer(
                            transcript
                        )
            if handle.transcript_path is None:
                raise PromptSubmissionUnobservable(
                    "AGENT_PROMPT_UNOBSERVED:{0} no transcript".format(name)
                )
            submit_agent_prompt(
                lambda *args, **kwargs: self._herdr(*args, env=environment, **kwargs),
                pane_id,
                bootstrap,
                name,
                timeout_s=60.0,
                until=("working", "idle"),
                working_proves=True,
                submission_recorded=_rising_submission_record(
                    handle, spec.prompt_path
                ),
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
            raise HandleAdoptionRefused("HANDLE_TOKEN_MISMATCH", token)
        with self._handles_lock:
            if self._handles.get(token) is not handle:
                raise HandleAdoptionRefused("HANDLE_NOT_OWNED", token)
        try:
            pane_payload = self._herdr(
                "pane", "get", handle.pane_id, env=handle.environment
            )
        except HerdrCallError as exc:
            if exc.code in (AGENT_NOT_FOUND, "pane_not_found"):
                raise HandleAbsent("PANE_ABSENT", handle.pane_id) from exc
            raise
        pane = _extract(pane_payload, "pane")
        if (
            not isinstance(pane, dict)
            or str(pane.get("pane_id") or "") != handle.pane_id
        ):
            raise HandleAdoptionRefused("PANE_ID_MISMATCH", handle.pane_id)
        cwd = pane.get("cwd")
        actual = Path(str(cwd)).resolve() if cwd else None
        if actual != handle.launched_cwd.resolve():
            raise HandleAdoptionRefused(
                "PANE_CWD_MISMATCH", "{}!={}".format(actual, handle.launched_cwd)
            )
        try:
            agent_payload = self._herdr(
                "agent", "get", handle.agent_name, env=handle.environment
            )
        except HerdrCallError as exc:
            if exc.code == AGENT_NOT_FOUND:
                raise HandleAbsent("AGENT_ABSENT", handle.agent_name) from exc
            raise
        agent = _extract(agent_payload, "agent")
        if not isinstance(agent, dict):
            raise HandleAbsent("AGENT_ABSENT", handle.agent_name)
        if str(agent.get("name") or "") != handle.agent_name:
            raise HandleAdoptionRefused("AGENT_ID_MISMATCH", handle.agent_name)
        agent_pane = agent.get("pane_id")
        if agent_pane is not None and str(agent_pane) != handle.pane_id:
            raise HandleAdoptionRefused(
                "AGENT_PANE_MISMATCH", "{}!={}".format(agent_pane, handle.pane_id)
            )

    def resubmit(
        self,
        handle: LaunchHandle,
        prompt_path: Path,
        *,
        route: str = "",
        expected_token: Optional[str] = None,
        timeout_s: float = 60.0,
    ) -> LaunchHandle:
        """Submit one new prompt to an already-owned interactive actor.

        The original pane, actor name, correlation token, and worktree binding
        are re-read before calling Herdr.  This deliberately returns the
        existing handle: a correction cycle has one actor session, rather than
        a sequence of visually similar replacement panes.
        """
        if expected_token is not None and handle.correlation_token != expected_token:
            raise HandleAdoptionRefused(
                "HANDLE_TOKEN_MISMATCH",
                "{}!={}".format(handle.correlation_token, expected_token),
            )
        prompt = Path(prompt_path)
        if not prompt.is_file():
            raise HandleAdoptionRefused("PROMPT_PATH_MISSING", str(prompt))
        if route:
            text = prompt.read_text(encoding="utf-8")
            prepared = prepare_route_prompt_text(route, text)
            if prepared != text:
                prompt.write_text(prepared, encoding="utf-8")
        self._verified_handle_binding(handle)
        wait_for_interactive_agent(
            lambda *args, **kwargs: self._herdr(
                *args, env=handle.environment, **kwargs
            ),
            handle.agent_name,
        )
        if handle.transcript_path is None:
            raise PromptSubmissionUnobservable(
                "AGENT_PROMPT_UNOBSERVED:{0} no transcript".format(handle.agent_name)
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
            until=("working", "idle"),
            working_proves=True,
            submission_recorded=_rising_submission_record(handle, prompt),
        )
        # A new prompt is a new turn. Any confirmation window still open from
        # the previous one measures a pane that has since been handed work,
        # and would convict this turn on the last one's silence.
        self._clear_quiescence(handle.correlation_token)
        return handle

    def _prove_tab_contains_pane(
        self,
        workspace_id: str,
        tab_id: str,
        pane_id: str,
        environment: Mapping[str, str],
    ) -> None:
        """Prove a pane's reported tab remains live without reading focus.

        `pane get` already reported `tab_id` for this exact pane; `tab list`
        proves that tab still exists in the same workspace.  Both facts are
        required before an absent agent may cause replacement, because opening
        a replacement in a freshly-created tab would silently split a lane.
        """
        try:
            payload = self._herdr(
                "tab", "list", "--workspace", workspace_id, env=environment
            )
        except HerdrCallError as exc:
            if exc.code == "workspace_not_found":
                self._invalidate_workspace_layout(workspace_id)
                raise HandleAbsent("WORKSPACE_ABSENT", workspace_id) from exc
            if exc.code == "tab_not_found":
                raise HandleAdoptionRefused("TAB_ABSENT", tab_id) from exc
            raise
        tabs = _extract(payload, "tabs")
        if not isinstance(tabs, list):
            raise HandleAdoptionRefused("TAB_LIST_UNPROVEN", tab_id)
        if any(
            isinstance(tab, dict) and str(tab.get("tab_id") or "") == tab_id
            for tab in tabs
        ):
            return
        raise HandleAdoptionRefused("TAB_ABSENT", tab_id)

    def restore_placement(
        self,
        *,
        workspace_id: str,
        lane_key: str,
        tab_id: str,
        pane_id: str,
        environment: Mapping[str, str],
    ) -> None:
        """Restore a run/lane layout from a durable pane without adopting its actor.

        Resume may need to launch a reviewer after the builder declaration has
        completed.  The builder actor token is then no longer the attempt token,
        so actor adoption is not the authority for locating the run's workspace.
        The persisted Herdr pane and tab IDs are.  Validate both against Herdr
        before caching them; a dead pane is typed absence, while mismatched IDs
        are a refusal rather than permission to create a second workspace.
        """
        workspace_id = str(workspace_id or "")
        lane_key = str(lane_key or "")
        tab_id = str(tab_id or "")
        pane_id = str(pane_id or "")
        if not workspace_id or not lane_key or not tab_id or not pane_id:
            raise HandleAdoptionRefused("PLACEMENT_ID_UNPROVEN", pane_id or tab_id)
        if (
            workspace_of(tab_id) != workspace_id
            or workspace_of(pane_id) != workspace_id
        ):
            raise HandleAdoptionRefused(
                "WORKSPACE_ID_MISMATCH",
                "{}!={}".format(workspace_id, workspace_of(pane_id)),
            )
        try:
            payload = self._herdr("pane", "get", pane_id, env=environment)
        except HerdrCallError as exc:
            if exc.code == "workspace_not_found":
                self._invalidate_workspace_layout(workspace_id)
                raise HandleAbsent("WORKSPACE_ABSENT", workspace_id) from exc
            if exc.code in (AGENT_NOT_FOUND, "pane_not_found"):
                raise HandleAbsent("PANE_ABSENT", pane_id) from exc
            raise
        pane = _extract(payload, "pane")
        if not isinstance(pane, dict) or str(pane.get("pane_id") or "") != pane_id:
            raise HandleAdoptionRefused("PANE_ID_MISMATCH", pane_id)
        pane_workspace = str(pane.get("workspace_id") or workspace_of(pane_id))
        pane_tab = str(pane.get("tab_id") or "")
        if pane_workspace != workspace_id:
            raise HandleAdoptionRefused(
                "WORKSPACE_ID_MISMATCH",
                "{}!={}".format(workspace_id, pane_workspace),
            )
        if pane_tab != tab_id:
            raise HandleAdoptionRefused(
                "TAB_ID_MISMATCH", "{}!={}".format(tab_id, pane_tab)
            )
        self._prove_tab_contains_pane(workspace_id, tab_id, pane_id, environment)
        with self._handles_lock:
            if self._workspace_id and self._workspace_id != workspace_id:
                raise HandleAdoptionRefused(
                    "WORKSPACE_ID_MISMATCH",
                    "{}!={}".format(self._workspace_id, workspace_id),
                )
            self._workspace_id = workspace_id
            layout = self._tabs.get(lane_key)
            if layout is not None and layout.tab_id != tab_id:
                raise HandleAdoptionRefused(
                    "TAB_ID_MISMATCH", "{}!={}".format(layout.tab_id, tab_id)
                )
            if layout is None:
                self._tabs[lane_key] = _TabLayout(
                    tab_id=tab_id, panes=[pane_id], claimed=1
                )

    def adopt(self, persisted: PersistedActorHandle) -> LaunchHandle:
        """Restore a handle only when persisted Herdr ids still prove ownership."""
        return self._adopt(persisted, allow_stale_placement=False)

    def retire_for_replacement(
        self, persisted: PersistedActorHandle, deadline: float
    ) -> None:
        """Close an owned actor whose durable placement belongs to an old run layout.

        Replacement is legal only after the pane cwd and deterministic actor identity
        still prove ownership.  Persisted workspace/tab mismatches are deliberately
        ignored here because they are the reason this actor must be retired.
        """
        try:
            handle = self._adopt(persisted, allow_stale_placement=True)
        except HandleAbsent:
            return
        self.cancel(handle, deadline)

    def close_actorless_pane(self, persisted: PersistedActorHandle) -> None:
        """Close the pane left after adoption proved its actor record absent."""
        if not self._reap_pane(persisted.pane_id, persisted.environment):
            raise HarnessQuiescenceError(
                "ACTORLESS_PANE_CLOSE_UNPROVEN:{}".format(persisted.pane_id)
            )

    def _adopt(
        self,
        persisted: PersistedActorHandle,
        *,
        allow_stale_placement: bool,
    ) -> LaunchHandle:
        """Validate one persisted actor, optionally outside the current run layout.

        Absence is a distinct typed outcome that allows the durable lifecycle
        to replace a generation. Transport/protocol failures and identity
        mismatches stay refusals: neither is evidence that the old actor died.
        """
        token = str(persisted.correlation_token or "")
        pane_id = str(persisted.pane_id or "")
        agent_name = str(persisted.agent_name or "")
        if (
            not token
            or not pane_id
            or not agent_name
            or agent_name != _agent_name(token)
        ):
            raise HandleAdoptionRefused("PERSISTED_IDENTITY_INVALID", token)
        environment = MappingProxyType(dict(persisted.environment))
        launched_cwd = Path(persisted.launched_cwd).resolve()
        try:
            pane_payload = self._herdr("pane", "get", pane_id, env=environment)
        except HerdrCallError as exc:
            if exc.code == "workspace_not_found":
                workspace_id = persisted.workspace_id or workspace_of(pane_id)
                self._invalidate_workspace_layout(workspace_id)
                raise HandleAbsent("WORKSPACE_ABSENT", workspace_id) from exc
            if exc.code in (AGENT_NOT_FOUND, "pane_not_found"):
                raise HandleAbsent("PANE_ABSENT", pane_id) from exc
            raise
        pane = _extract(pane_payload, "pane")
        if not isinstance(pane, dict) or str(pane.get("pane_id") or "") != pane_id:
            raise HandleAdoptionRefused("PANE_ID_MISMATCH", pane_id)
        cwd = pane.get("cwd")
        actual = Path(str(cwd)).resolve() if cwd else None
        if actual != launched_cwd:
            raise HandleAdoptionRefused(
                "PANE_CWD_MISMATCH", "{}!={}".format(actual, launched_cwd)
            )
        pane_workspace = str(pane.get("workspace_id") or "")
        persisted_tab_id = str(persisted.tab_id or "")
        pane_tab_id = str(pane.get("tab_id") or "")
        if allow_stale_placement:
            if not pane_tab_id:
                raise HandleAdoptionRefused("TAB_ID_UNPROVEN", pane_id)
            tab_id = pane_tab_id
        elif persisted_tab_id:
            if pane_tab_id != persisted_tab_id:
                raise HandleAdoptionRefused(
                    "TAB_ID_MISMATCH",
                    "{}!={}".format(pane_tab_id, persisted_tab_id),
                )
            tab_id = persisted_tab_id
        elif pane_tab_id:
            # Legacy rows may have no tab id.  A direct pane response is the
            # only recovery authority; workspace or focus never fills it in.
            tab_id = pane_tab_id
        else:
            raise HandleAdoptionRefused("TAB_ID_UNPROVEN", pane_id)
        lane_key = str(persisted.lane_key or "")
        if not lane_key:
            raise HandleAdoptionRefused("LANE_KEY_UNPROVEN", pane_id)
        pane_id_workspace = workspace_of(pane_id)
        tab_workspace = workspace_of(tab_id)
        workspace_id = str(
            (pane_workspace or pane_id_workspace)
            if allow_stale_placement
            else (persisted.workspace_id or pane_workspace or pane_id_workspace)
        )
        if (
            not workspace_id
            or workspace_id != pane_id_workspace
            or workspace_id != tab_workspace
            or (pane_workspace and pane_workspace != workspace_id)
        ):
            raise HandleAdoptionRefused(
                "WORKSPACE_ID_MISMATCH",
                "{}!={}".format(workspace_id, pane_workspace or pane_id_workspace),
            )
        self._prove_tab_contains_pane(workspace_id, tab_id, pane_id, environment)
        candidate = LaunchHandle(
            correlation_token=token,
            pane_id=pane_id,
            agent_name=agent_name,
            launched_cwd=launched_cwd,
            transcript_path=persisted.transcript_path,
            envelope_path=persisted.envelope_path,
            environment=environment,
            workspace_id=workspace_id,
            tab_id=tab_id,
            lane_key=lane_key,
        )
        # The pane's ID/cwd are already proven.  Keep that verified placement
        # even if Herdr then proves the actor absent, so a replacement opens
        # in this lane's workspace/tab rather than creating a second workspace.
        with self._handles_lock:
            existing = self._handles.get(token)
            if existing is not None and existing != candidate:
                raise HandleAdoptionRefused("HANDLE_TOKEN_COLLISION", token)
            if not allow_stale_placement:
                if self._workspace_id and self._workspace_id != workspace_id:
                    raise HandleAdoptionRefused(
                        "WORKSPACE_ID_MISMATCH",
                        "{}!={}".format(self._workspace_id, workspace_id),
                    )
                if not self._workspace_id:
                    self._workspace_id = workspace_id
                layout = self._tabs.get(lane_key)
                if layout is None:
                    self._tabs[lane_key] = _TabLayout(
                        tab_id=tab_id, panes=[pane_id], claimed=1
                    )
                elif layout.tab_id != tab_id:
                    raise HandleAdoptionRefused(
                        "TAB_ID_MISMATCH",
                        "{}!={}".format(layout.tab_id, tab_id),
                    )
                else:
                    with layout.lock:
                        if pane_id not in layout.panes:
                            try:
                                slot = layout.panes.index(None)
                                layout.panes[slot] = pane_id
                            except ValueError:
                                slot = len(layout.panes)
                                layout.panes.append(pane_id)
                            layout.claimed = max(layout.claimed, slot + 1)
                            layout.cols = max(
                                layout.cols,
                                grid_for(layout.claimed)[1],
                            )
        try:
            agent_payload = self._herdr("agent", "get", agent_name, env=environment)
        except HerdrCallError as exc:
            if exc.code == AGENT_NOT_FOUND:
                raise HandleAbsent("AGENT_ABSENT", agent_name) from exc
            raise
        agent = _extract(agent_payload, "agent")
        if not isinstance(agent, dict):
            raise HandleAbsent("AGENT_ABSENT", agent_name)
        if str(agent.get("name") or "") != agent_name:
            raise HandleAdoptionRefused("AGENT_ID_MISMATCH", agent_name)
        agent_pane = agent.get("pane_id")
        if agent_pane is not None and str(agent_pane) != pane_id:
            raise HandleAdoptionRefused(
                "AGENT_PANE_MISMATCH", "{}!={}".format(agent_pane, pane_id)
            )
        with self._handles_lock:
            self._handles[token] = candidate
            self._proven_absent.pop(token, None)
            self._quiescent_since.pop(token, None)
            if candidate.transcript_path is not None:
                self._tailers[token] = TranscriptTailer(candidate.transcript_path)
        return candidate

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
        if status == AGENT_QUIESCENT_STATUS and turns:
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

    def reclaim(self, token: str) -> Tuple[LaunchHandle, ...]:
        with self._handles_lock:
            handle = self._handles.get(token)
        return (handle,) if handle is not None else ()

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
        return True if isinstance(_extract(payload, "agent"), dict) else None

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
        handle = LaunchHandle(
            spec.correlation_token,
            "fake:" + spec.correlation_token,
            _agent_name(spec.correlation_token),
            spec.worktree.resolve(),
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
    ) -> LaunchHandle:
        del route, timeout_s
        if expected_token is not None and expected_token != handle.correlation_token:
            raise HandleAdoptionRefused("HANDLE_TOKEN_MISMATCH")
        if self._handles.get(handle.correlation_token) is not handle:
            raise HandleAdoptionRefused("HANDLE_NOT_OWNED")
        if not Path(prompt_path).is_file():
            raise HandleAdoptionRefused("PROMPT_PATH_MISSING")
        self._states[handle.correlation_token] = PollResult(PollState.RUNNING)
        return handle

    def adopt(self, persisted: PersistedActorHandle) -> LaunchHandle:
        handle = self._handles.get(persisted.correlation_token)
        if handle is None:
            raise HandleAbsent("AGENT_ABSENT", persisted.agent_name)
        if (
            handle.pane_id != persisted.pane_id
            or handle.agent_name != persisted.agent_name
            or handle.launched_cwd != persisted.launched_cwd.resolve()
        ):
            raise HandleAdoptionRefused("PERSISTED_IDENTITY_INVALID")
        return handle

    def retire_for_replacement(
        self, persisted: PersistedActorHandle, deadline: float
    ) -> None:
        handle = self.adopt(persisted)
        self.cancel(handle, deadline)

    def close_actorless_pane(self, persisted: PersistedActorHandle) -> None:
        if persisted.correlation_token in self._handles:
            raise HarnessQuiescenceError(
                "ACTOR_STILL_PRESENT:{}".format(persisted.correlation_token)
            )

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
        self._states[handle.correlation_token] = PollResult(
            PollState.GONE, detail="CANCELLED"
        )

    def reclaim(self, token: str) -> Tuple[LaunchHandle, ...]:
        handle = self._handles.get(token)
        return (handle,) if handle is not None else ()

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
