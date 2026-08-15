"""Per-attempt worktrees, the measurement bracket, and the merge protocol (§8).

This module owns one definition of an attempt's identity, one definition of
what was measured, and one path by which measured content becomes a commit on
the integration branch. Everything here is deliberately a function of state
that harness-controlled execution created and measured, never of a name, a
pattern, or a tool's own report about itself.

The shape of an attempt, in the order the caller must execute it:

    attempt = create_attempt_worktree(...)   # §8.1, §8.2 — from the integration head
    check_at_create(attempt)                 # §8.3's four checks, first evaluation
    <adapter runs provision, then the pre-node gate, and confirms both dead>
    baseline = take_baseline(attempt)        # §8.3 — the bracket opens here
    <the agent works, or the code node's command runs, and settles>
    after = inventory(attempt.path)          # §8.3 — the bracket closes here
    d = delta(baseline, after)
    permission_check(attempt, d, declared)   # §8.3's two conjuncts (§7.3 clause 4)
    sha = commit_measured_delta(attempt, d, after, msg)   # §8.4, before the gate
    check_post_commit(attempt, expected_inventory(baseline, d, after))
    run_node_gate(attempt, cmd, selector, cancel_requested)  # §7.3 clause 3
    merge_verified_node(integration, node_id, sha)        # §8.5, §8.6

Two things this module deliberately does not do, so that no caller can mistake
their absence for their presence (§12.3 — a deferral is loud, never a stub):

* **Quiesce.** §8.3 requires provision, the pre-gate, the post-gate, and a code
  node's own command to run as harness-owned process groups that are confirmed
  terminated before the adjacent measurement is trusted. Process-group
  ownership belongs to the runner adapter and the launcher (§9.3, §12.2 step
  7), so it is not implemented here and is not faked here: `take_baseline` and
  the after-inventory measure whatever the tree holds when they are called, and
  it is the caller's obligation to have terminated the previous context first.
* **Provision.** The ecosystem's setup command is adapter code (§9.3). This
  module only states where in the order it belongs.

The two corrections below are executed findings from the composition probe of
2026-08-13, not preferences:

* The cleanliness evaluations compare against the **expected full inventory**
  (baseline ∪ the measured committed delta), never against `git status`. As
  originally worded, §8.3's check convicted two fully-verified nodes over a
  byproduct the pre-gate itself created and the baseline deliberately included
  (§16.3 item 36). Nothing is permitted by name: a relocated or renamed
  byproduct is judged on its own measured tuple and still convicts.
* The per-node gate is **scoped to the node's own selector**. Running the whole
  suite as a post-node gate makes a node red because a sibling's not-yet-merged
  work is absent from its worktree, so under §7.3 no node could ever verify.
  Only the final integration gate (§8.8) is whole-suite, and it has its own
  function so that it is an explicit choice rather than a default.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .launcher import (
    HarnessCancelled,
    HarnessQuiescenceError,
    run_harness_process,
)

# An inventory maps a worktree-relative path to its tuple: git's mode class and
# git's blob object id for the bytes at that path (§8.3).
InventoryTuple = Tuple[str, str]
Inventory = Dict[str, InventoryTuple]

# git's own committable resolution, and nothing finer. Ownership, timestamps,
# and permission bits below git's model are invisible here exactly as they are
# invisible to git, so the inventory can never measure a delta the commit
# cannot stage (§8.3).
MODE_SYMLINK = "120000"
MODE_EXECUTABLE = "100755"
MODE_REGULAR = "100644"

# States that end a node without a merge. A node in one of these is excluded
# from the frontier and cascades to its descendants for every reason, not only
# for conflict (§8.5, §8.7).
TERMINAL_WITHOUT_MERGE = ("BLOCKED", "CANCELLED")


class WorktreeError(RuntimeError):
    """Anything the worktree protocol refuses to do, with git's own reason."""


class BranchCollision(WorktreeError):
    """`git worktree add -b` refused an existing branch — §8.2's collision guard.

    The attempt tuple is in the branch name, so this means either two runs of
    one plan digest collided or an attempt number was reused. Both are bugs in
    the caller's identity, which is why the guard is git's rather than ours.
    """


class HeadMoved(WorktreeError):
    """HEAD no longer equals the attempt's recorded base at commit time.

    §8.4's pre-commit assertion: an agent that committed on its own is caught
    here rather than by the compare-and-swap below. The assertion is early
    detection, not the enforcement — the enforcement is the private index and
    the compare-and-swap, both of which hold even if this check were removed.
    """


class CompareAndSwapRefused(WorktreeError):
    """The attempt ref moved under the harness, so the swap refused (§8.4).

    This is the failure a survivor's own `git commit` produces. It fails the
    attempt ENVIRONMENTAL: something wrote git state in a window where nothing
    is permitted to write, which is a fact about the machine and not a verdict
    about the work.
    """


class StagingMismatch(WorktreeError):
    """A staged path's index entry differs from its after-inventory tuple.

    §8.4's staging assertion. git re-hashes the working-tree bytes when it
    populates the private index, so this is a genuinely fresh second
    measurement of the same path rather than a restatement of the first, and a
    write landing between the after-inventory and staging surfaces here instead
    of passing a tautology. Also fails the attempt ENVIRONMENTAL.
    """

class GateCancelled(WorktreeError):
    """A gate stopped without a result after its owned process group quiesced."""




# ── git plumbing ────────────────────────────────────────────────────────────

def _git(cwd: Path, *args: str, env: Optional[Mapping[str, str]] = None,
         check: bool = True, stdin: Optional[str] = None) -> "subprocess.CompletedProcess":
    """One git invocation. Every git call in this module goes through here.

    `env` is merged over the process environment rather than replacing it,
    because git needs the ambient PATH and the caller's identity configuration
    to produce a commit at all; the only variable this module ever injects is
    `GIT_INDEX_FILE`, and it injects it for exactly the calls that must not
    touch the worktree's own index.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=({**os.environ, **env} if env else None),
        input=stdin,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} in {cwd} -> {result.returncode}: {result.stderr.strip()}")
    return result


def _out(cwd: Path, *args: str, env: Optional[Mapping[str, str]] = None) -> str:
    return _git(cwd, *args, env=env).stdout.strip()


def integration_head(repo: Path, branch: str) -> str:
    """The **execution** base: the current head of the integration branch (§8.1).

    Deliberately not `base_commit`, which is the *authoring* base and is a
    claim about truth at a different commit. Under a base-commit rule a
    dependent node never sees its dependency's merged output, so its
    differential gate is red at both ends and `needs` is inert.
    """
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}",
                  check=False)
    if result.returncode != 0:
        raise WorktreeError(f"integration branch {branch!r} does not resolve in {repo}")
    return result.stdout.strip()

_OBJECT_DIGEST = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def is_valid_output_commit(repo: Path, output_sha: str,
                           expected_base: Optional[str] = None) -> bool:
    """Whether a durable output identity names the recorded commit.

    A lifecycle row is authority only after its SHA has the canonical object
    shape, resolves as a commit in this repository, and — for a still
    VERIFIED attempt — descends from that attempt's immutable execution base.
    """
    if not _OBJECT_DIGEST.fullmatch(output_sha):
        return False
    repo = Path(repo)
    if _git(repo, "cat-file", "-e", f"{output_sha}^{{commit}}",
            check=False).returncode != 0:
        return False
    return (expected_base is None
            or _git(repo, "merge-base", "--is-ancestor", expected_base, output_sha,
                    check=False).returncode == 0)


def is_attempt_output_commit(repo: Path, output_sha: str, *, run_id: str,
                             node_id: str, attempt_no: int,
                             expected_base: str) -> bool:
    """Whether SHA is the exact commit published by this durable attempt ref.

    An ancestry predicate alone accepts a later descendant that a forged
    lifecycle row can name.  The attempt branch is created from the recorded
    base and advanced once by compare-and-swap when the private-index commit is
    made, so its exact ref is the durable identity that binds the row to the
    attempt that produced it.
    """
    if not is_valid_output_commit(repo, output_sha, expected_base=expected_base):
        return False
    ref = "refs/heads/{}".format(branch_name(run_id, node_id, attempt_no))
    resolved = _git(
        Path(repo), "rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(ref),
        check=False)
    return resolved.returncode == 0 and resolved.stdout.strip() == output_sha


# ── §8.2 identity ───────────────────────────────────────────────────────────

def branch_name(run_id: str, node_id: str, attempt_no: int) -> str:
    """`maestro/{run_id}/{node_id}/a{attempt_no}` — one definition, here (§8.2)."""
    return f"maestro/{run_id}/{node_id}/a{attempt_no}"


def worktree_dirname(run_id: str, node_id: str, attempt_no: int) -> str:
    """The worktree directory carries the same tuple as the branch (§8.2).

    A directory cannot hold the branch's slashes, so the separator differs and
    nothing else does; both names still encode the attempt, which is what makes
    attempt 2 a different worktree instead of a collision.
    """
    return f"{run_id}-{node_id}-a{attempt_no}"


@dataclass
class AttemptWorktree:
    """One attempt's worktree, branch, recorded base, and measured baseline."""

    repo: Path
    path: Path
    branch: str
    base: str
    scratch: Path
    private_index: Path
    run_id: str
    node_id: str
    attempt_no: int
    tracked_at_base: frozenset
    baseline: Optional[Inventory] = None

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"


def create_worktree(repo: Path, path: Path, branch: str, base: str) -> Path:
    """`git worktree add -b` — the branch creation *is* the collision guard (§8.2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _git(repo, "worktree", "add", "-q", "-b", branch, str(path), base,
                  check=False)
    if result.returncode != 0:
        message = result.stderr.strip()
        if "already exists" in message:
            raise BranchCollision(
                f"branch {branch!r} or worktree {path} already exists: {message}")
        raise WorktreeError(f"git worktree add failed: {message}")
    return path


def create_attempt_worktree(repo: Path, run_id: str, node_id: str, attempt_no: int,
                            integration_head: str, worktrees_root: Path,
                            scratch_root: Path) -> AttemptWorktree:
    """Create the attempt's worktree and branch from the integration head (§8.1).

    The scratch directory and the harness-private index are both allocated
    here, and both live outside the worktree. The index location is not
    stylistic: an index inside the worktree — or inside the scratch the gates
    also write to — becomes a path in the very delta it is measuring, and
    convicts the node it was created to commit (§8.4, probe finding F4).
    """
    repo = Path(repo).resolve()
    worktrees_root = Path(worktrees_root).resolve()
    path = worktrees_root / worktree_dirname(run_id, node_id, attempt_no)
    branch = branch_name(run_id, node_id, attempt_no)
    create_worktree(repo, path, branch, integration_head)

    scratch = Path(scratch_root).resolve() / worktree_dirname(run_id, node_id, attempt_no)
    scratch.mkdir(parents=True, exist_ok=True)
    private_index = worktrees_root / f".index-{worktree_dirname(run_id, node_id, attempt_no)}"

    tracked = frozenset(line for line in _out(path, "ls-files").splitlines() if line)
    return AttemptWorktree(
        repo=repo, path=path.resolve(), branch=branch,
        base=_out(path, "rev-parse", "HEAD"), scratch=scratch,
        private_index=private_index, run_id=run_id, node_id=node_id,
        attempt_no=attempt_no, tracked_at_base=tracked,
    )


# ── §8.3 the inventory tuple ────────────────────────────────────────────────

def _mode_class(path: Path) -> str:
    """git's committable mode resolution for one path."""
    if path.is_symlink():
        return MODE_SYMLINK
    return MODE_EXECUTABLE if path.lstat().st_mode & 0o111 else MODE_REGULAR


def _blob_ids(worktree: Path, relpaths: Sequence[str]) -> List[str]:
    """git's blob object ids for regular files, in the order given.

    One `git hash-object --stdin-paths` invocation hashes the whole batch, so a
    provisioned tree costs one subprocess rather than one per file. The hash is
    git's own, computed through git's own attribute and filter resolution for
    that path, which is exactly what makes an inventory tuple directly
    comparable to the index entry §8.4's staging assertion reads back.

    A path containing a newline cannot be expressed on `--stdin-paths` input,
    so those few fall back to a per-path invocation rather than being skipped:
    an unmeasurable path would be a hole in the bracket.
    """
    if not relpaths:
        return []
    inline = [rel for rel in relpaths if "\n" not in rel]
    ids: Dict[str, str] = {}
    if inline:
        out = _git(worktree, "hash-object", "-t", "blob", "--stdin-paths",
                   stdin="\n".join(inline) + "\n").stdout.split()
        if len(out) != len(inline):
            raise WorktreeError("git hash-object returned a mismatched number of ids")
        ids.update(zip(inline, out))
    for rel in relpaths:
        if rel not in ids:
            ids[rel] = _out(worktree, "hash-object", "-t", "blob", "--", rel)
    return [ids[rel] for rel in relpaths]


def _symlink_blob_id(worktree: Path, relpath: str) -> str:
    """A symlink hashes its own target-path bytes, never the dereferenced file.

    This is what makes "a file replaced by a symlink to identical bytes" a
    tuple change rather than an invisible one (§8.3).
    """
    target = os.readlink(worktree / relpath)
    return _git(worktree, "hash-object", "-t", "blob", "--stdin",
                stdin=target).stdout.strip()


def inventory(worktree: Path) -> Inventory:
    """Every path in the worktree, tracked and untracked alike, as tuples (§8.3).

    No excludes and no ignore list: the baseline's inclusion of untracked
    content is load-bearing, because it is what lets conjunct (2) convict
    tampering with provisioned content that no declared glob covers. The single
    omission is git's own `.git` administrative path, which git can never
    commit and which is therefore not content any delta could carry — omitting
    it is a statement about what git tracks, not a permitted set.

    Both inventories hash every path in full. A stat-based short-circuit is
    forbidden here (§8.3): an agent that rewrites a provisioned file and
    restores its stat metadata defeats a stat cache, which is the precise hole
    the content hash was chosen to close.
    """
    worktree = Path(worktree)
    regular: List[str] = []
    inv: Inventory = {}
    for dirpath, dirnames, filenames in os.walk(worktree, followlinks=False):
        here = Path(dirpath)
        kept = []
        for name in dirnames:
            if name == ".git":
                continue
            if (here / name).is_symlink():
                # A symlink to a directory is content, not a directory to walk.
                rel = str((here / name).relative_to(worktree))
                inv[rel] = (MODE_SYMLINK, _symlink_blob_id(worktree, rel))
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if name == ".git":
                continue
            full = here / name
            rel = str(full.relative_to(worktree))
            if full.is_symlink():
                inv[rel] = (MODE_SYMLINK, _symlink_blob_id(worktree, rel))
            else:
                regular.append(rel)
    for rel, blob in zip(regular, _blob_ids(worktree, regular)):
        inv[rel] = (_mode_class(worktree / rel), blob)
    return inv


@dataclass(frozen=True)
class InventoryDelta:
    """What the bracket measured: added, changed, and removed paths (§8.3)."""

    added: Tuple[str, ...] = ()
    changed: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()

    @property
    def touched(self) -> Tuple[str, ...]:
        return tuple(sorted(set(self.added) | set(self.changed) | set(self.removed)))

    @property
    def is_empty(self) -> bool:
        return not self.touched


def delta(before: Inventory, after: Inventory) -> InventoryDelta:
    """The measured delta between two inventories — the commit set (§8.3)."""
    return InventoryDelta(
        added=tuple(sorted(k for k in after if k not in before)),
        changed=tuple(sorted(k for k in after if k in before and after[k] != before[k])),
        removed=tuple(sorted(k for k in before if k not in after)),
    )


def take_baseline(attempt: AttemptWorktree) -> Inventory:
    """Open the bracket: the provisioned tree, not the bare checkout (§8.3).

    The caller must already have run `git worktree add`, the adapter's
    `provision`, and the pre-node gate, and must have confirmed that both of
    those execution contexts are dead (§8.3's symmetric quiesce, which lives in
    the launcher — see this module's header). Ecosystem setup is execution-base
    construction like the checkout itself, and the pre-gate is the harness's
    own trusted execution; neither is the node's work, and measuring them as if
    they were is what convicted diligent agents in the earlier design.
    """
    attempt.baseline = inventory(attempt.path)
    return attempt.baseline


def expected_inventory(baseline: Inventory, measured: InventoryDelta,
                       after: Inventory) -> Inventory:
    """The baseline unioned with the measured, committed delta (§8.3, item 36).

    This is what "clean" means at both cleanliness evaluations. It is built
    from the baseline and the delta rather than simply returned as the
    after-inventory, because that construction is the claim being made: every
    path in the expected state is either something harness-controlled execution
    put there before any untrusted code ran, or something the commit carried.
    """
    expected: Inventory = dict(baseline)
    for rel in measured.added + measured.changed:
        if rel not in after:
            raise WorktreeError(f"delta names {rel!r} but the after-inventory has no tuple")
        expected[rel] = after[rel]
    for rel in measured.removed:
        expected.pop(rel, None)
    return expected


# ── §8.3 cache redirection ──────────────────────────────────────────────────

def scratch_env(scratch: Path) -> Dict[str, str]:
    """Point every cache this harness knows about at the attempt's scratch (§8.3).

    Byproducts are redirected out of the worktree, not suppressed tool by tool:
    an unknown tool likely honours an XDG or tmp convention, while a tool
    honouring neither fails closed as a loud, path-named conviction whose fix
    is one variable in one adapter.

    This is load-bearing rather than tidy. In the 2026-08-13 composition probe
    the same worktree measured 16 baseline paths without this environment and 6
    with it, and without it an agent that merely ran its own tests was
    convicted for the bytecode cache its test run wrote.

    The same environment must be applied to **all** the contexts that execute
    inside or around the bracket — the pane environment at allocation (§9.3),
    the adapter's own gate invocation, and a code node's command (§7.3) — which
    is why this returns the mapping instead of applying it: only the caller
    knows which process it is about to start.
    """
    scratch = Path(scratch)
    return {
        "XDG_CACHE_HOME": str(scratch / "xdg"),
        "TMPDIR": str(scratch / "tmp"),
        "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
        "PYTEST_ADDOPTS": f"-o cache_dir={scratch / 'pytest_cache'}",
        "COVERAGE_FILE": str(scratch / "coverage"),
        "RUFF_CACHE_DIR": str(scratch / "ruff"),
        "npm_config_cache": str(scratch / "npm"),
    }


def launch_env(scratch: Path, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A complete environment for anything launched inside the bracket.

    The scratch subdirectories are created here because a redirection to a
    directory that does not exist is a redirection some tools ignore, and an
    ignored redirection is indistinguishable from no redirection until it
    convicts someone.
    """
    env = dict(os.environ if base is None else base)
    redirected = scratch_env(scratch)
    for key in ("XDG_CACHE_HOME", "TMPDIR", "PYTHONPYCACHEPREFIX", "npm_config_cache"):
        Path(redirected[key]).mkdir(parents=True, exist_ok=True)
    Path(redirected["PYTEST_ADDOPTS"].split("=", 1)[1]).mkdir(parents=True, exist_ok=True)
    env.update(redirected)
    return env


# ── §8.3 the two-conjunct permission check ──────────────────────────────────

def _matches_any(relpath: str, declared: Iterable[str]) -> bool:
    """Whether a measured path is covered by one of the node's declared outputs.

    Declared outputs are name-shaped (§8.3) and matched as globs. `fnmatch`'s
    `*` crosses a path separator, which makes this matcher slightly *more*
    permissive than a POSIX shell glob; that looseness is bounded by conjunct
    (2) below, which no glob can satisfy away.
    """
    return any(relpath == pattern or fnmatch.fnmatchcase(relpath, pattern)
               for pattern in declared)


@dataclass(frozen=True)
class PermissionVerdict:
    """§8.3's two conjuncts, both computed from measured state."""

    passes: bool
    conjunct1_violations: Tuple[str, ...] = ()
    conjunct2_violations: Tuple[str, ...] = ()


def permission_check(attempt: AttemptWorktree, measured: InventoryDelta,
                     declared: Sequence[str]) -> PermissionVerdict:
    """Conjunct (1) subset-of-declared, conjunct (2) provisioned content (§8.3).

    Declared outputs are name-shaped and the provisioned base is
    content-shaped, and conjunct (2) is what keeps the name-shaped half from
    authorizing what the baseline exists to protect: a glob can authorize
    creating a new file or changing committed content, and can never authorize
    tampering with provisioned untracked content. Modifying or deleting a
    baseline-present untracked path convicts unconditionally, whatever the
    globs say — which is what turns "a stubbed dependency is a detected
    violation" from an assumption about plan hygiene into a theorem.
    """
    if attempt.baseline is None:
        raise WorktreeError("permission_check before take_baseline — the bracket never opened")

    c1 = tuple(rel for rel in measured.touched if not _matches_any(rel, declared))
    c2: List[str] = []
    for rel in measured.changed + measured.removed:
        if rel not in attempt.tracked_at_base:
            c2.append(f"modified or deleted but untracked at the attempt's base: {rel}")
    for rel in measured.added:
        if rel in attempt.baseline:
            c2.append(f"added but already present in the baseline inventory: {rel}")
    return PermissionVerdict(passes=not c1 and not c2,
                             conjunct1_violations=c1,
                             conjunct2_violations=tuple(c2))


# ── §8.4 the scheduler-side commit ──────────────────────────────────────────

def _is_inside(child: Path, parent: Path) -> bool:
    return str(child).startswith(str(parent) + os.sep)


def _index_entries(attempt: AttemptWorktree, env: Mapping[str, str]) -> Inventory:
    """The private index's own view of what was staged: mode and blob id.

    `-z` because a path with a space or a newline must survive this readback;
    quoting it would make the staging assertion lie about exactly the paths
    most worth checking.
    """
    raw = _git(attempt.path, "ls-files", "--stage", "-z", env=env).stdout
    entries: Inventory = {}
    for record in raw.split("\0"):
        if not record:
            continue
        meta, _, rel = record.partition("\t")
        mode, blob, _stage = meta.split()
        entries[rel] = (mode, blob)
    return entries


def advance_attempt_ref(attempt: AttemptWorktree, output_sha: str) -> None:
    """Move the attempt ref from the recorded base by compare-and-swap (§8.4).

    The expected old value is the attempt's recorded base, so a survivor's own
    commit — which moved the ref out from under the harness — makes this refuse
    rather than build on top of it.
    """
    result = _git(attempt.repo, "update-ref", attempt.ref, output_sha, attempt.base,
                  check=False)
    if result.returncode != 0:
        raise CompareAndSwapRefused(
            f"{attempt.ref} no longer at the recorded base {attempt.base[:10]}: "
            f"{result.stderr.strip()}")


def commit_measured_delta(attempt: AttemptWorktree, measured: InventoryDelta,
                          after: Inventory, message: str) -> str:
    """Commit exactly the measured delta, from a harness-private index (§8.4).

    The scheduler commits, never the agent, and it commits from git state the
    harness alone writes: staging runs against a private index outside the
    worktree and outside the shared scratch, so a survivor's `git add -A`
    mutates an index this path never reads. The commit object is produced by
    `write-tree` and `commit-tree` with the recorded base as its sole parent —
    never by `git commit` against whatever HEAD and the shared index hold — and
    the ref advances by compare-and-swap.

    The commit is taken **before** the post-node gate runs, which is what makes
    "nothing the gate writes can reach the integration branch" true by
    construction rather than by hygiene: the merge consumes committed objects.

    A node with an empty delta commits an empty commit on purpose, so every
    node has an output SHA and the merge guard stays uniform (§8.4).
    """
    if attempt.baseline is None:
        raise WorktreeError("commit_measured_delta before take_baseline")
    if _is_inside(attempt.private_index, attempt.path) or \
            _is_inside(attempt.private_index, attempt.scratch):
        raise WorktreeError(
            f"the private index {attempt.private_index} is inside the worktree or the "
            "shared scratch, which would make the index itself a delta path (§8.4)")

    head = _out(attempt.path, "rev-parse", "HEAD")
    if head != attempt.base:
        raise HeadMoved(
            f"HEAD is {head[:10]}, not the attempt's recorded base {attempt.base[:10]} — "
            "something committed in this worktree that was not the harness")

    attempt.private_index.parent.mkdir(parents=True, exist_ok=True)
    attempt.private_index.unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": str(attempt.private_index)}
    try:
        # Seed the private index from the recorded base, then apply only the delta.
        _git(attempt.path, "read-tree", attempt.base, env=env)
        for rel in measured.added + measured.changed:
            _git(attempt.path, "update-index", "--add", "--", rel, env=env)
        for rel in measured.removed:
            _git(attempt.path, "update-index", "--force-remove", "--", rel, env=env)

        # §8.4's staging assertion: a second, independent measurement of the
        # same paths, taken by git at staging time, must equal the tuples the
        # after-inventory measured. A write inside the window where nothing may
        # write surfaces here instead of passing silently into the commit.
        staged = _index_entries(attempt, env)
        for rel in measured.added + measured.changed:
            if staged.get(rel) != after.get(rel):
                raise StagingMismatch(
                    f"{rel} staged as {staged.get(rel)} but measured as {after.get(rel)} — "
                    "something wrote to the tree between the after-inventory and staging")
        for rel in measured.removed:
            if rel in staged:
                raise StagingMismatch(f"{rel} was measured as deleted but is still staged")

        tree = _out(attempt.path, "write-tree", env=env)
        output_sha = _out(attempt.path, "commit-tree", tree, "-p", attempt.base,
                          "-m", message, env=env)
    finally:
        attempt.private_index.unlink(missing_ok=True)

    advance_attempt_ref(attempt, output_sha)

    # The private index left a debt: the worktree's own index still holds the
    # base tree, so every reader of worktree dirtiness would see the harness's
    # own bookkeeping as the node's dirt. Refreshing it is safe precisely
    # because the commit is already sealed and this module still never *reads*
    # that index — content flows only through the private index, the commit
    # object, and the SHA the merge consumes (§8.4).
    _git(attempt.path, "read-tree", output_sha)
    return output_sha


# ── §8.3 the four checks ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Divergence:
    """One path where the measured tree differs from the expected inventory."""

    path: str
    kind: str                      # "unexpected", "missing", or "changed"
    expected: Optional[InventoryTuple] = None
    actual: Optional[InventoryTuple] = None


@dataclass(frozen=True)
class CleanlinessVerdict:
    """The comparison against the expected full inventory (§8.3, §16.3 item 36).

    `consequence` is "convict" at the post-commit evaluation and "report" at
    pre-merge, because by then the commit is sealed and the merge consumes
    committed objects, so residue is an adapter hygiene defect with its paths
    named rather than a verdict about the node's work.
    """

    clean: bool
    consequence: str
    divergences: Tuple[Divergence, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    """§8.3's four checks at one of their three evaluation points."""

    stage: str
    branch_checked_out: bool
    head_resolves: bool
    base_is_ancestor: bool
    cleanliness: Optional[CleanlinessVerdict]
    ok: bool
    merge_permitted: bool
    detail: Tuple[str, ...] = ()


def compare_to_expected(worktree: Path, expected: Inventory,
                        consequence: str) -> CleanlinessVerdict:
    """Compare the tree's full measured inventory to the expected one (§8.3).

    This is a state comparison, not a `git status` read and not an ignore list.
    Nothing is permitted by name: a path the expected inventory already carries
    convicts no one for merely still being present — a pre-gate byproduct the
    baseline measured is part of what "expected" means — while any path whose
    measured tuple diverges convicts whatever it is called, including the same
    byproduct renamed, relocated, or rewritten.
    """
    actual = inventory(worktree)
    divergences: List[Divergence] = []
    for rel in sorted(set(actual) | set(expected)):
        if rel not in expected:
            divergences.append(Divergence(rel, "unexpected", None, actual[rel]))
        elif rel not in actual:
            divergences.append(Divergence(rel, "missing", expected[rel], None))
        elif actual[rel] != expected[rel]:
            divergences.append(Divergence(rel, "changed", expected[rel], actual[rel]))
    return CleanlinessVerdict(clean=not divergences, consequence=consequence,
                              divergences=tuple(divergences))


def _git_checks(attempt: AttemptWorktree) -> Tuple[bool, bool, bool, Tuple[str, ...]]:
    """The three git-side checks: branch checked out, HEAD resolves, ancestry."""
    detail: List[str] = []
    branch = _git(attempt.path, "symbolic-ref", "--short", "HEAD", check=False)
    branch_checked_out = branch.returncode == 0 and branch.stdout.strip() == attempt.branch
    if not branch_checked_out:
        detail.append(f"HEAD is not on {attempt.branch}: {branch.stdout.strip() or 'detached'}")

    head = _git(attempt.path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}",
                check=False)
    head_resolves = head.returncode == 0
    if not head_resolves:
        detail.append("HEAD does not resolve to a commit")

    base_is_ancestor = False
    if head_resolves:
        base_is_ancestor = _git(attempt.path, "merge-base", "--is-ancestor",
                                attempt.base, "HEAD", check=False).returncode == 0
        if not base_is_ancestor:
            detail.append(f"the recorded base {attempt.base[:10]} is not an ancestor of HEAD")
    return branch_checked_out, head_resolves, base_is_ancestor, tuple(detail)


def check_at_create(attempt: AttemptWorktree) -> CheckResult:
    """The first of the three evaluation points: nothing has run yet (§8.3).

    Cleanliness is not evaluated here — there is no committed delta to expect
    yet, and the run-level precondition that the repository's working tree is
    clean before any node starts belongs to §8.2, not to this bracket.
    """
    branch_ok, head_ok, ancestor_ok, detail = _git_checks(attempt)
    ok = branch_ok and head_ok and ancestor_ok
    return CheckResult("create", branch_ok, head_ok, ancestor_ok, None, ok, ok, detail)


def check_post_commit(attempt: AttemptWorktree, expected: Inventory) -> CheckResult:
    """Immediately after the commit and before the post-node gate runs (§8.3).

    A divergence from the expected full inventory **convicts**: nothing but the
    harness has executed between the after-inventory and §8.4's index refresh,
    so a genuine divergence is a stray write. Baseline-present content the
    delta never touched is part of the expected inventory and is not dirt —
    which is the half of §13.3's negative control that the original
    `git status` formulation could not pass.
    """
    branch_ok, head_ok, ancestor_ok, detail = _git_checks(attempt)
    cleanliness = compare_to_expected(attempt.path, expected, "convict")
    ok = branch_ok and head_ok and ancestor_ok and cleanliness.clean
    return CheckResult("post-commit", branch_ok, head_ok, ancestor_ok, cleanliness,
                       ok, ok, detail)


def check_pre_merge(attempt: AttemptWorktree, expected: Inventory) -> CheckResult:
    """After the post-node gate has run, immediately before the merge (§8.3).

    Residue here is **reported** as an adapter hygiene defect with the paths
    named rather than convicting the node: the commit was sealed before the
    gate ran and the merge consumes committed objects, so a post-gate rewrite
    is a maintenance signal about the adapter and never a merge hazard.
    """
    branch_ok, head_ok, ancestor_ok, detail = _git_checks(attempt)
    cleanliness = compare_to_expected(attempt.path, expected, "report")
    git_ok = branch_ok and head_ok and ancestor_ok
    return CheckResult("pre-merge", branch_ok, head_ok, ancestor_ok, cleanliness,
                       git_ok, git_ok, detail)


# ── gates: node-scoped by default, whole-suite only as integration ──────────

_COUNT = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped)")


@dataclass(frozen=True)
class GateResult:
    """One gate execution. §10.2's counting rule adjudicates a parsed report;
    what this records is the execution itself — command, scope, exit code."""

    label: str
    scope: str                     # "node" or "integration"
    selector: Optional[str]
    command: Tuple[str, ...]
    exit_code: int
    green: bool
    counts: Dict[str, int] = field(default_factory=dict)
    tail: Tuple[str, ...] = ()


def _run_gate(
        worktree: Path, command: Sequence[str], scratch: Path, label: str,
        scope: str, selector: Optional[str],
        cancel_requested: Callable[[], bool],
) -> GateResult:
    try:
        result = run_harness_process(
            command, cwd=worktree, env=launch_env(scratch),
            cancel_requested=cancel_requested)
    except HarnessCancelled as exc:
        raise GateCancelled("gate cancelled before a result was produced") from exc
    output = (result.stdout or "") + (result.stderr or "")
    counts = {("error" if key.startswith("error") else key): int(value)
              for value, key in _COUNT.findall(output)}
    return GateResult(label=label, scope=scope, selector=selector,
                      command=tuple(command), exit_code=result.returncode,
                      green=result.returncode == 0, counts=counts,
                      tail=tuple(output.strip().splitlines()[-5:]))


def run_node_gate(attempt: AttemptWorktree, command: Sequence[str], selector: str,
                  cancel_requested: Callable[[], bool],
                  label: str = "node-gate") -> GateResult:
    """Run a gate scoped to the node's own declared selector (§7.4, §7.3).

    The selector is required, and that is the F3 correction made structural.
    Run whole-suite here instead and a node goes red because a sibling's
    not-yet-merged work is absent from its worktree — the composition probe
    executed exactly that — so under §7.3 clause 3 no node could ever reach
    VERIFIED, nothing would merge, and the design would deadlock. Whole-suite
    execution has its own function below, so choosing it is explicit.

    The gate inherits the bracket's redirected environment, which is not
    optional: a gate run without it writes its own caches into the worktree it
    is judging.
    """
    if not selector or not str(selector).strip():
        raise ValueError(
            "a node gate needs the node's own selector; whole-suite execution is "
            "run_integration_gate (§8.8), never an unscoped node gate")
    return _run_gate(attempt.path, list(command) + [selector], attempt.scratch,
                     label, "node", selector, cancel_requested)


def run_integration_gate(
        worktree: Path, command: Sequence[str], scratch: Path,
        cancel_requested: Callable[[], bool],
        label: str = "integration-gate",
) -> GateResult:
    """Run the final whole-suite gate under the caller's cancellation lease.

    This is the only gate deliberately unscoped: it judges the integrated tree,
    where semantic conflicts between individually-correct nodes become visible.
    """
    return _run_gate(
        Path(worktree), command, Path(scratch), label, "integration", None,
        cancel_requested)


# ── §8.5 deterministic merge order ──────────────────────────────────────────

@dataclass(frozen=True)
class NodeRecord:
    """What merge ordering needs to know about a node — graph facts, not timing."""

    node_id: str
    depth: int
    needs: Tuple[str, ...] = ()
    state: str = "PENDING"
    specs: Tuple[str, ...] = ()

    def with_state(self, state: str) -> "NodeRecord":
        return replace(self, state=state)


def merge_frontier(nodes: Iterable[NodeRecord]) -> Tuple[str, ...]:
    """The eligible frontier, ordered by `(depth, node_id)` (§8.5).

    The frontier is a function of the graph, the merged set, and the blocked
    set only — never of finish order — so two runs with inverted completion
    produce an identical merge sequence. Selecting from the *verified* subset
    instead would make order depend on timing.

    Excluding terminal-without-merge nodes is load-bearing rather than tidy:
    without it the merge thread waits forever on a node that will never verify,
    verified independent nodes queue behind it, and the run consumes its whole
    budget having merged nothing (§8.5).
    """
    records = list(nodes)
    merged = {n.node_id for n in records if n.state == "MERGED"}
    eligible = [n for n in records
                if n.state != "MERGED"
                and n.state not in TERMINAL_WITHOUT_MERGE
                and all(dep in merged for dep in n.needs)]
    return tuple(n.node_id for n in sorted(eligible, key=lambda n: (n.depth, n.node_id)))


def next_merge_candidate(nodes: Iterable[NodeRecord]) -> Optional[NodeRecord]:
    """The frontier minimum, whatever its state — the node the merge thread waits on."""
    records = {n.node_id: n for n in nodes}
    frontier = merge_frontier(records.values())
    return records[frontier[0]] if frontier else None


def merge_ready(nodes: Iterable[NodeRecord]) -> Optional[NodeRecord]:
    """The frontier minimum if it is VERIFIED, else None — head-of-line waiting.

    Returning None rather than the next verified node is the whole point: the
    wait is cheap because a frontier node has all its dependencies merged and
    is running or about to, and skipping it would make order depend on timing.
    """
    candidate = next_merge_candidate(nodes)
    if candidate is not None and candidate.state == "VERIFIED":
        return candidate
    return None


def upstream_blocked(nodes: Iterable[NodeRecord]) -> Tuple[str, ...]:
    """Nodes with a blocked or abandoned ancestor — derived, never stored (§8.7).

    Storing this made the cascade irreversible: an operator escape that
    un-blocks the origin left its descendants sitting in a durable terminal
    state with no rule anywhere to bring them back, so the rescue rescued
    nothing. Derived, the origin simply leaves the blocked set and the
    predicate stops holding on the next frontier computation.
    """
    records = {n.node_id: n for n in nodes}
    origins = {n.node_id for n in records.values() if n.state in TERMINAL_WITHOUT_MERGE}
    affected = set()
    changed = True
    while changed:
        changed = False
        for node in records.values():
            if node.node_id in origins or node.node_id in affected:
                continue
            if any(dep in origins or dep in affected for dep in node.needs):
                affected.add(node.node_id)
                changed = True
    return tuple(sorted(affected))


# ── §8.6 ancestry, §8.7 conflict ────────────────────────────────────────────

@dataclass(frozen=True)
class MergeResult:
    """One merge transaction's outcome, guarded by git's exit code alone (§8.6)."""

    node_id: str
    output_sha: str
    head_before: str
    head_after: str
    merged_sha: Optional[str] = None
    ancestry_proven: bool = False
    conflicted_paths: Tuple[str, ...] = ()
    detail: str = ""


def merge_verified_node(integration_path: Path, node_id: str, output_sha: str,
                        message: Optional[str] = None) -> MergeResult:
    """Merge one verified node's output SHA into the integration worktree.

    Checked **and merged** on the SHA, never on the branch: a branch is a
    mutable pointer, and a merge by name would carry a survivor's later commit
    while the ancestry proof still passed, because the recorded output SHA is
    an ancestor of its own descendants.

    On conflict the conflicted paths are captured, the merge is aborted so the
    integration head is byte-identical to before, and the caller blocks the node
    with that evidence while independent branches keep running (§8.7).
    Resolution is human: a conflict means two output sets overlapped in content
    though their declared globs did not, which is a planning defect that
    re-prompting papers over.
    """
    integration_path = Path(integration_path)
    head_before = _out(integration_path, "rev-parse", "HEAD")
    merge = _git(integration_path, "merge", "--no-ff", "--no-edit",
                 "-m", message or f"merge {node_id}", output_sha, check=False)

    if merge.returncode != 0:
        conflicted = tuple(sorted(
            line for line in _out(integration_path, "diff", "--name-only",
                                  "--diff-filter=U").splitlines() if line))
        _git(integration_path, "merge", "--abort", check=False)
        head_after = _out(integration_path, "rev-parse", "HEAD")
        if head_after != head_before:
            raise WorktreeError(
                f"aborting the merge of {node_id} left the integration head at "
                f"{head_after[:10]} instead of {head_before[:10]}")
        return MergeResult(node_id=node_id, output_sha=output_sha,
                           head_before=head_before, head_after=head_after,
                           conflicted_paths=conflicted,
                           detail=merge.stderr.strip() or merge.stdout.strip())

    ancestry = _git(integration_path, "merge-base", "--is-ancestor", output_sha, "HEAD",
                    check=False).returncode == 0
    head_after = _out(integration_path, "rev-parse", "HEAD")
    return MergeResult(node_id=node_id, output_sha=output_sha, head_before=head_before,
                       head_after=head_after, merged_sha=head_after if ancestry else None,
                       ancestry_proven=ancestry)


# ── §8.8 acceptance and cleanup ─────────────────────────────────────────────

def final_ancestry_sweep(integration_path: Path,
                         output_shas: Mapping[str, str]) -> Dict[str, bool]:
    """Re-prove every merged node against the **final** head (§8.6).

    A run is not accepted otherwise, regardless of green tests: test PASS is
    structurally never merge provenance, so the only evidence that a node's
    content is in the final tree is git's own ancestry answer at the end.
    """
    integration_path = Path(integration_path)
    return {node_id: _git(integration_path, "merge-base", "--is-ancestor", sha, "HEAD",
                          check=False).returncode == 0
            for node_id, sha in output_shas.items()}


def acceptance_specs(nodes: Iterable[NodeRecord]) -> Tuple[str, ...]:
    """The deduplicated union of every **merged** node's declared specs (§8.8).

    Derived rather than hand-authored, and sorted so that two runs of one plan
    produce the same acceptance set. A blocked node's specs are excluded
    because its content is not in the tree being accepted. The union alone is
    not sufficient — §8.8 adds one whole-suite integration gate, which is
    `run_integration_gate` above, precisely because a semantic conflict is
    invisible to the specs that failed to name it.
    """
    merged = [n for n in nodes if n.state == "MERGED"]
    return tuple(sorted({spec for node in merged for spec in node.specs}))


def remove_attempt_worktree(attempt: AttemptWorktree, ancestry_proven: bool,
                            integration_path: Optional[Path] = None) -> None:
    """Remove the worktree and its branch, only after ancestry is proven (§8.8).

    Deleting an unmerged branch destroys the only copy of the work, so an
    unproven ancestry is a refusal rather than a warning. Nothing here forces:
    `git worktree remove` without `--force` and `git branch -d` are both
    allowed to refuse, and their refusal is surfaced as an error rather than
    overridden — a worktree that will not come away cleanly is evidence about
    the attempt, and a blocked node's worktree is retained for post-mortem by
    simply never calling this.

    The branch deletion runs in the integration worktree when one is given,
    because `git branch -d` asks whether the branch is merged into the *current*
    HEAD, and the integration branch is the head that merged it.
    """
    if not ancestry_proven:
        raise WorktreeError(
            f"refusing to remove {attempt.path}: ancestry is not proven, and deleting "
            "an unmerged branch destroys the only copy of the work (§8.8)")
    _git(attempt.repo, "worktree", "remove", str(attempt.path))
    _git(Path(integration_path) if integration_path else attempt.repo,
         "branch", "-d", attempt.branch)
