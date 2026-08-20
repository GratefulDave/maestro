"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these

The second half of this module is the capability axis rather than the write
axis, and it is enforced at dispatch rather than after the fact — see
`route_capability_argv` for why the two are enforced at opposite ends.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, Sequence, Tuple

from .data_types import AgentConfig, SSSFConfig


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def snapshot(run) -> dict[str, str]:
    """Fingerprint every path the working tree currently differs on.

    Tracked files carry their numstat counts, so an edit to an already-dirty
    file still registers as a change. Untracked files are listed by name.
    Gitignored paths never appear, which is why the session runtime under
    `data_dir` — where handoff files legitimately land — needs no special case.
    """
    fingerprints: dict[str, str] = {}
    for line in _git(["diff", "HEAD", "--numstat"], run.repo_root).splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            path = fields[-1].strip()
            fingerprints[path] = f"{fields[0]},{fields[1]}"
    for path in _git(["ls-files", "--others", "--exclude-standard"],
                     run.repo_root).splitlines():
        if path.strip():
            fingerprints[path.strip()] = "untracked"
    return fingerprints


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(cfg: SSSFConfig) -> list[str]:
    """The session runtime, which EVERY agent must be able to write.

    `context_handoff/` is the one place agents hand work to each other, and an
    agent's own prompts, raw_output.jsonl, and envelope.json land beside it.
    Scout writes its findings there, the reviewer its review, the planner its
    plan — a read-only agent is read-only with respect to the REPO, never with
    respect to its own report.

    This is granted from `data_dir` rather than left to .gitignore. The runtime
    is normally ignored, so it never even appears in a snapshot — but an agent's
    ability to record its work must not hang on a gitignore entry that someone
    can delete or that a changed `data_dir` can outgrow.
    """
    return [cfg.defaults.data_dir.rstrip("/") + "/"]


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig) -> bool:
    """Session runtime first, then the agent's own list, then what is protected."""
    if any(_matches(path, p) for p in always_writable(cfg)):
        return True
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _roll_back(run, path: str, before: dict[str, str], after: dict[str, str]) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.
    """
    if path in before:
        # Already dirty beforehand. If it is gone from the diff now, the agent
        # reverted an engineer's uncommitted work and the content is not ours
        # to reconstruct — say so loudly rather than pretend it was handled.
        return "REVERTED-BY-AGENT (uncommitted work lost, cannot restore)" \
            if path not in after else "left as-is (was already modified)"
    if after.get(path) == "untracked":
        try:
            (Path(run.repo_root) / path).unlink()
            return "deleted"
        except OSError as error:
            return f"could not delete ({error})"
    result = subprocess.run(["git", "checkout", "--", path],
                            cwd=run.repo_root, capture_output=True, text=True)
    return "rolled back" if result.returncode == 0 else "could not roll back"


def enforce(run, phase, agent: AgentConfig, before: dict[str, str]) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.
    """
    after = snapshot(run)
    touched = changed_paths(before, after)
    breaches = [p for p in touched if not permitted(p, agent, run.cfg)]
    if not breaches:
        return touched

    outcomes = {p: _roll_back(run, p, before, after) for p in breaches}
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files}")
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")


# ── The capability axis: what an actor may INVOKE ──────────────────────────
#
# Everything above answers "what may this agent change", after the fact, from
# the repository itself. This answers a question the write axis structurally
# cannot: what may this agent *delegate*. A write is visible in a diff. Work
# handed to a sub-agent is not — it lands in the actor's own transcript as its
# own output, and the receipt the actor then signs names the actor.
#
# §3.6 B12 requires that no actor sign off on its own output and that review be
# cross-vendor over the merged surface. §1.2 forbids any lifecycle transition
# caused by an agent's claim about its own work. A reviewer that spawns a
# sub-task, waits for it, and relays its conclusion satisfies neither: the
# verdict originates somewhere the signature does not name, and nothing
# downstream can tell the difference, because the transcript, the receipt and
# the signature all say the actor Maestro launched.
#
# This is not hypothetical and it is not rare. In
# run-2a44d226e75a4be391a14f02b78a6d25, 23 of 39 launched reviews spawned a
# named sub-task through omp's `hub`/`task` tools and blocked on it; 17 of
# those went on to write a signed receipt attesting
# `{"model": "openai-codex/gpt-5.6-luna"}` over a verdict produced by a
# `gpt-5.6-terra` sub-task, three of them PASS. The remaining six were SIGHUPed
# mid-`hub op=wait`, wrote no receipt at all, and were absorbed as
# environmental stalls that cancelled and rebuilt the *builder* (#94, #90,
# #101).
#
# **Why this axis is enforced at dispatch and the write axis is not.** A write
# can be undone: `enforce` rolls the path back and fails the phase, and the
# repository is the durable record either way. A delegated judgement cannot be
# undone after the fact, because by the time anything could look, the only
# surviving artifact is a signed receipt that says the right actor produced it.
# So the capability is removed before the process starts rather than convicted
# afterwards — the actor never holds the tool.
#
# **What this does and does not contain.** It is the same honest limit §9.6
# draws around `--dangerously-skip-permissions`: this contains a *mistaken*
# actor, not a hostile one. `bash` remains on every allowlist below, and an
# actor determined to reach a second model can shell out to one. What it stops
# is the ordinary case — the reviewer that reaches for the delegation tool
# sitting in its schema because the tool is there.

#: Per route, the tool names that let an actor hand its work to another model.
#: omp's `task` spawns a sub-agent and `hub` starts and awaits named background
#: jobs; both are in that binary's `BUILTIN_TOOL_NAMES`, so both are nameable
#: in `--tools`. Claude's equivalents are `Task` and its current spelling
#: `Agent`; both are named because a deny list that names only the retired
#: spelling denies nothing.
DELEGATION_TOOLS: Dict[str, Tuple[str, ...]] = {
    "omp": ("task", "hub"),
    "claude": ("Task", "Agent"),
}

#: Per route, the built-in tools a Maestro-launched actor may reach, before
#: `DELEGATION_TOOLS` is subtracted. Only omp has one: `--tools` there is
#: validated against the session's fully discovered registry, so an allowlist
#: naming a tool the profile did not register is a startup error rather than a
#: silent no-op — which makes an allowlist safe to state and a typo loud.
#:
#: The membership is measured, not guessed. Across the 39 reviewer sessions and
#: 48 builder sessions of run-2a44d226e75a4be391a14f02b78a6d25 the tools
#: actually invoked were, reviewer: hub, read, write, task, bash, grep, eval;
#: builder: read, glob, write, grep, todo, bash, edit, eval. This set is the
#: union of both minus the two delegation tools, so no actor loses a capability
#: it was observed to use. `lsp` is deliberately absent: `--no-lsp` and a
#: profile setting can both unregister it, and naming an unregistered tool
#: fails the launch.
ROUTE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "omp": ("read", "write", "edit", "bash", "grep", "glob", "todo", "eval"),
}


def route_delegation_tools(route: str) -> Tuple[str, ...]:
    """The tools `route` must not hand an actor. Empty for an unknown route."""
    return DELEGATION_TOOLS.get(route, ())


def route_tool_allowlist(route: str) -> Tuple[str, ...]:
    """`route`'s permitted tools, with its delegation tools filtered out.

    The subtraction happens here rather than in the table above, so adding a
    delegation tool to `ROUTE_TOOLS` cannot quietly re-grant it. That is the
    B15 shape applied to this module's own constants: the invariant is a
    computation, not an agreement between two lists someone must keep level.

    Empty means no allowlist is expressible for this route — either the route
    is unknown, or its CLI has no enumerable built-in set worth stating. The
    caller falls back to `route_delegation_tools` as a deny list.
    """
    denied = set(route_delegation_tools(route))
    return tuple(t for t in ROUTE_TOOLS.get(route, ()) if t not in denied)


def route_capability_argv(route: str) -> Tuple[str, ...]:
    """The argv fragment that denies `route`'s actor the ability to delegate.

    Two shapes, because the two CLIs differ in what they can be told:

    * **omp** takes an allowlist (`--tools read,write,...`), validated against
      the session registry. Stating the whole permitted set is stronger than
      naming what to remove: a tool that appears in a future omp release, or
      one an extension registers, is absent by default rather than newly
      reachable.
    * **claude** takes a deny list (`--disallowedTools Task Agent`). Its
      built-in set is not enumerable from here with the confidence omp's is,
      and an allowlist that omitted a tool the plan author needs would break
      the author lane rather than contain it. Naming the two delegation tools
      is the change that is provably no wider than the defect.

    An unknown route yields no flags. That is not a silent pass: an unknown
    route never reaches an argv builder at all — `HerdrLauncher.launch` refuses
    it `UNSUPPORTED_ROUTE` before this could matter — so the empty answer is
    unreachable rather than permissive.
    """
    allowed = route_tool_allowlist(route)
    if allowed:
        return ("--tools", ",".join(allowed))
    denied = route_delegation_tools(route)
    if denied:
        return ("--disallowedTools", *denied)
    return ()


def argv_denies_delegation(route: str, argv: Sequence[str]) -> bool:
    """Whether `argv` leaves `route`'s actor unable to delegate its work.

    A detector, in §13.4's sense: production never calls it, tests assert it
    over the real `launcher.build_omp_argv` / `build_claude_argv` output and
    over a planted violation. It exists as a named object rather than as an
    assertion inside one test file because B15's lesson is precisely that an
    invariant living only inside a test dies with that test — Gates 1-10 were
    deleted by the cutover commit that orphaned their fields.

    True requires one of the two containments to be *stated in the argv*:
    either an allowlist that names no delegation tool, or a deny list that
    names every one of them. An argv carrying neither is unconstrained, which
    is the state every Maestro launch was in before this module grew.
    """
    denied = route_delegation_tools(route)
    if not denied:
        return True
    tokens = list(argv)
    for index, token in enumerate(tokens):
        if token == "--tools" and index + 1 < len(tokens):
            allowed = {t.strip() for t in tokens[index + 1].split(",")}
            if allowed and not (allowed & set(denied)):
                return True
    for index, token in enumerate(tokens):
        if token in ("--disallowedTools", "--disallowed-tools"):
            if set(denied) <= set(tokens[index + 1:]):
                return True
    return False
