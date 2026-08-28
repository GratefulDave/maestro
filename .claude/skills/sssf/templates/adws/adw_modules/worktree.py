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
    run_node_gate(attempt, resolved_runner, argv, selector,
                  cancel_requested)                    # §7.3 clause 3
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
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .launcher import (
    SCRATCH_ENV_KEYS,
    HarnessCancelled,
    HarnessQuiescenceError,
    pytest_worker_cap,
    run_harness_process,
)
from . import runner_resolution as rr

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


class InvalidCandidateParent(WorktreeError):
    """A next-candidate cycle did not name one canonical commit object."""


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


def _git(
    cwd: Path,
    *args: str,
    env: Optional[Mapping[str, str]] = None,
    check: bool = True,
    stdin: Optional[str] = None,
) -> "subprocess.CompletedProcess":
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
            f"git {' '.join(args)} in {cwd} -> {result.returncode}: {result.stderr.strip()}"
        )
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
    result = _git(
        repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}", check=False
    )
    if result.returncode != 0:
        raise WorktreeError(f"integration branch {branch!r} does not resolve in {repo}")
    return result.stdout.strip()


def resolve_commit(repo: Path, spec: str) -> str:
    """The commit object a revision names, whatever spelling recorded it.

    `rev-parse <spec>^{commit}` peels tags and abbreviated SHAs to the
    object id. Spelling is not identity: `abc1234` and the full SHA are
    the same commit when they resolve to the same object.
    """
    result = _git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        "{0}^{{commit}}".format(spec),
        check=False,
    )
    if result.returncode != 0:
        raise WorktreeError("revision {0!r} does not resolve in {1}".format(spec, repo))
    return result.stdout.strip()


_OBJECT_DIGEST = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def is_object_digest(value: str) -> bool:
    """Whether a string has the canonical git object shape — sha1 or sha256.

    Public because a caller that refuses a SHA needs to say *which* thing was
    wrong with it. `is_valid_output_commit` collapses shape, existence and
    ancestry into one boolean, which is right for a predicate and useless for
    a diagnostic: an abbreviated SHA failed the shape test and was reported to
    the operator as an ancestry failure, about a commit that descended from
    the named base perfectly well (#78).
    """
    return _OBJECT_DIGEST.fullmatch(value) is not None


def is_valid_output_commit(
    repo: Path, output_sha: str, expected_base: Optional[str] = None
) -> bool:
    """Whether a durable output identity names the recorded commit.

    A lifecycle row is authority only after its SHA has the canonical object
    shape, resolves as a commit in this repository, and — for a still
    VERIFIED attempt — descends from that attempt's immutable execution base.
    """
    if not _OBJECT_DIGEST.fullmatch(output_sha):
        return False
    repo = Path(repo)
    if (
        _git(repo, "cat-file", "-e", f"{output_sha}^{{commit}}", check=False).returncode
        != 0
    ):
        return False
    return (
        expected_base is None
        or _git(
            repo, "merge-base", "--is-ancestor", expected_base, output_sha, check=False
        ).returncode
        == 0
    )


def is_attempt_output_commit(
    repo: Path,
    output_sha: str,
    *,
    run_id: str,
    node_id: str,
    attempt_no: int,
    expected_base: str,
) -> bool:
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
        Path(repo),
        "rev-parse",
        "--verify",
        "--quiet",
        "{}^{{commit}}".format(ref),
        check=False,
    )
    return resolved.returncode == 0 and resolved.stdout.strip() == output_sha


def attempt_ref_commit(
    repo: Path, run_id: str, node_id: str, attempt_no: int
) -> Optional[str]:
    """The commit this attempt's own durable ref holds, or `None` if absent.

    The surviving-commit read. `commit_measured_delta` advances
    `refs/heads/maestro/{run}/{node}/a{n}` by compare-and-swap, so this ref is
    where a builder's work outlives the process that produced it — and asking
    it *again*, after some later step failed, is the difference between "the
    tree is still there" and an in-memory SHA a caller happens to remember.
    Issue #90's re-review of a stalled reviewer's subject reads it; so does
    salvage, which is why it lives here rather than privately in either.

    §7.5's git rule is enforced, not approximated. Only `rev-parse`'s
    documented exit 1 means the ref is absent; every other nonzero exit is a
    fact about the machine and raises, because reporting a transient git
    failure as "the commit is gone" is how a caller decides to discard work
    that is sitting on disk.
    """
    ref = "refs/heads/{}".format(branch_name(run_id, node_id, attempt_no))
    resolved = _git(
        Path(repo),
        "rev-parse",
        "--verify",
        "--quiet",
        "{}^{{commit}}".format(ref),
        check=False,
    )
    if resolved.returncode == 0:
        return resolved.stdout.strip()
    if resolved.returncode == 1:
        return None
    raise WorktreeError(
        "git rev-parse of {0} exited {1}: {2}".format(
            ref, resolved.returncode, resolved.stderr.strip()
        )
    )


def is_attempt_lineage_commit(
    repo: Path,
    output_sha: str,
    *,
    run_id: str,
    node_id: str,
    attempt_no: int,
    expected_base: str,
) -> bool:
    """Whether SHA is a published commit retained by this attempt's ref.

    Persistent builders advance one attempt ref for every immutable candidate.
    Recovery may therefore find the last published candidate behind a newer,
    not-yet-published repair commit.  The candidate remains owned by the
    attempt only while it is an ancestor of that attempt's current ref.
    """
    if not is_valid_output_commit(repo, output_sha, expected_base=expected_base):
        return False
    tip = attempt_ref_commit(repo, run_id, node_id, attempt_no)
    if tip is None:
        return False
    return (
        _git(
            Path(repo),
            "merge-base",
            "--is-ancestor",
            output_sha,
            tip,
            check=False,
        ).returncode
        == 0
    )


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
    #: Content digests of the on-disk paths `inventory()` cannot see at the
    #: moment the bracket opened — the provisioned gitignored tree. It is the
    #: before-side `existing_ignored_outputs` needs to tell a node's ignored
    #: write apart from a dependency install's, and like `baseline` it is
    #: recorded by `take_baseline` and left `None` by `reopen_attempt_worktree`,
    #: because a commit cannot rebuild content no commit holds.
    ignored_at_base: Optional[Dict[str, str]] = None

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"


def create_worktree(repo: Path, path: Path, branch: str, base: str) -> Path:
    """`git worktree add -b` — the branch creation *is* the collision guard (§8.2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _git(
        repo, "worktree", "add", "-q", "-b", branch, str(path), base, check=False
    )
    if result.returncode != 0:
        message = result.stderr.strip()
        if "already exists" in message:
            raise BranchCollision(
                f"branch {branch!r} or worktree {path} already exists: {message}"
            )
        raise WorktreeError(f"git worktree add failed: {message}")
    return path


def create_attempt_worktree(
    repo: Path,
    run_id: str,
    node_id: str,
    attempt_no: int,
    integration_head: str,
    worktrees_root: Path,
    scratch_root: Path,
) -> AttemptWorktree:
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

    scratch = Path(scratch_root).resolve() / worktree_dirname(
        run_id, node_id, attempt_no
    )
    scratch.mkdir(parents=True, exist_ok=True)
    private_index = (
        worktrees_root / f".index-{worktree_dirname(run_id, node_id, attempt_no)}"
    )

    tracked = frozenset(line for line in _out(path, "ls-files").splitlines() if line)
    return AttemptWorktree(
        repo=repo,
        path=path.resolve(),
        branch=branch,
        base=_out(path, "rev-parse", "HEAD"),
        scratch=scratch,
        private_index=private_index,
        run_id=run_id,
        node_id=node_id,
        attempt_no=attempt_no,
        tracked_at_base=tracked,
    )


def inventory_at_commit(repo: Path, sha: str) -> Inventory:
    """The recorded base tree as inventory tuples — what git *tracked* at `sha`.

    This is the durable source for `tracked_at_base`, and nothing else. It is
    **not** the measurement baseline and must never be substituted for one:
    §8.3's baseline is the provisioned tree, which deliberately keeps
    untracked non-ignored content in scope so conjunct (2) can convict
    tampering with it. A commit holds no untracked path, so the two universes
    differ by exactly the provisioned untracked set — and reading this as the
    baseline reports every one of those as a path the attempt added. The
    baseline is recorded when the bracket opens and read back from the ledger;
    it is not reconstructable from git.
    """
    result = _git(Path(repo), "ls-tree", "-r", "-z", sha, check=False)
    if result.returncode != 0:
        raise WorktreeError(
            f"recorded base {sha} cannot be read as a tree: {result.stderr.strip()}"
        )
    inv: Inventory = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        meta, _, rel = record.partition("\t")
        parts = meta.split()
        if len(parts) != 3:
            raise WorktreeError(f"ls-tree record is not mode/type/blob: {record!r}")
        mode, kind, blob = parts
        if kind != "blob":
            continue
        if mode not in (MODE_REGULAR, MODE_EXECUTABLE, MODE_SYMLINK):
            continue
        inv[rel] = (mode, blob)
    return inv


def attempt_worktree_exists(
    worktrees_root: Path, run_id: str, node_id: str, attempt_no: int
) -> bool:
    """Whether this exact generation's checkout is still on disk.

    `reopen_attempt_worktree` refuses a missing one and `create_attempt_worktree`
    refuses an existing one, so the caller that may meet either -- a same-attempt
    recovery, whose checkout §8.8's cleanup may or may not have removed -- has to
    ask before it chooses. Asked of the path the two of them compute the same way,
    rather than of git's worktree list: an entry git still administers for a
    directory that is gone is not a checkout anything can reopen.
    """
    path = Path(worktrees_root).resolve() / worktree_dirname(
        run_id, node_id, attempt_no
    )
    return path.is_dir()


def reopen_attempt_worktree(
    repo: Path,
    run_id: str,
    node_id: str,
    attempt_no: int,
    base: str,
    worktrees_root: Path,
    scratch_root: Path,
) -> AttemptWorktree:
    """Rebuild the attempt handle from durable identity. Does not create.

    Salvage has to measure in a1's own worktree against a1's own recorded
    base. Creating a new worktree would mint attempt 2's bracket.

    **`baseline` is left `None`, and that is the contract.** The base commit
    can rebuild `tracked_at_base` — tracking is a property of the commit — but
    it cannot rebuild the baseline, which §8.3 defines as the *provisioned*
    tree and which therefore holds untracked paths no commit contains.
    Handing back `inventory_at_commit` as if it were the baseline made every
    provisioned untracked path read as a path the attempt added; where one was
    covered by a declared output it was committed as the attempt's measured
    delta and signed for (§1.1 item 4). A caller that needs the before-side
    must supply the one that was recorded when the bracket opened
    (`LifecycleStore.attempt_baseline`), and `permission_check` and
    `commit_measured_delta` both refuse a `None` baseline rather than reading
    it as an empty one — an empty baseline reads *everything* as added, which
    is worse than the substitution it replaces.
    """
    repo = Path(repo).resolve()
    worktrees_root = Path(worktrees_root).resolve()
    path = worktrees_root / worktree_dirname(run_id, node_id, attempt_no)
    if not path.is_dir():
        raise WorktreeError(f"attempt worktree {path} is gone")
    branch = branch_name(run_id, node_id, attempt_no)
    scratch = Path(scratch_root).resolve() / worktree_dirname(
        run_id, node_id, attempt_no
    )
    scratch.mkdir(parents=True, exist_ok=True)
    private_index = (
        worktrees_root / f".index-{worktree_dirname(run_id, node_id, attempt_no)}"
    )
    tracked_at_base = frozenset(inventory_at_commit(repo, base))
    return AttemptWorktree(
        repo=repo,
        path=path.resolve(),
        branch=branch,
        base=base,
        scratch=scratch,
        private_index=private_index,
        run_id=run_id,
        node_id=node_id,
        attempt_no=attempt_no,
        tracked_at_base=tracked_at_base,
        baseline=None,
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
        out = _git(
            worktree,
            "hash-object",
            "-t",
            "blob",
            "--stdin-paths",
            stdin="\n".join(inline) + "\n",
        ).stdout.split()
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
    return _git(
        worktree, "hash-object", "-t", "blob", "--stdin", stdin=target
    ).stdout.strip()


def blob_text(worktree: Path, blob: str) -> Optional[str]:
    """One blob's contents as text, or `None` when it is not text.

    The baseline inventory records a blob id per path, so a file the attempt
    *changed* can be compared against the bytes it started from without keeping
    a second copy of the tree.

    `None` means exactly one thing — the object exists and is not decodable as
    UTF-8, so no parser will read it — and it never means the read failed.
    §7.5: only git's documented not-found exit means an object is absent, and
    `cat-file` has none, so a nonzero exit here is environmental and raises.
    The id came from an inventory git itself produced, which makes a failure to
    read it a fact about the machine rather than about the tree; resolving it
    to `None` would let a transient git failure read as "this file had no
    prior version", and a caller comparing against a base would then attribute
    the whole file to the attempt that touched one line of it.
    """
    try:
        result = _git(worktree, "cat-file", "blob", blob, check=False)
    except UnicodeDecodeError:
        return None
    if result.returncode != 0:
        raise WorktreeError(
            f"git cat-file blob {blob} in {worktree} -> {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _committable_paths(worktree: Path) -> List[str]:
    """git's own enumeration of this working tree, as relative paths (§8.3).

    `--cached` is every path in the index — including one git tracks in spite
    of an ignore rule, because tracking is what settles membership, not the
    rule. `--others --exclude-standard` adds the untracked paths git would
    still consider part of the tree, and excludes the ones it would not:
    `.gitignore`, `.git/info/exclude`, and `core.excludesFile` alike, resolved
    by git rather than re-implemented here. `.git` never appears.

    `-z` because a path containing a newline must survive this readback;
    quoting it would make the inventory lie about exactly the paths most worth
    measuring. `--full-name` pins the paths to the tree's top so the caller's
    working directory cannot shift what a relative path means.
    """
    raw = _git(
        worktree,
        "ls-files",
        "-z",
        "--full-name",
        "--cached",
        "--others",
        "--exclude-standard",
    ).stdout
    return [rel for rel in raw.split("\0") if rel]


def inventory(worktree: Path) -> Inventory:
    """Every path git considers part of the worktree, as tuples (§8.3).

    The universe is git's, not the filesystem's. A git-ignored path is not
    content this bracket can measure, commit, or merge: §8.4 stages the
    measured delta into a private index and §8.5 merges the resulting commit,
    so a path git will not carry cannot reach the integration branch no matter
    what any inventory said about it. Enumerating it anyway does not make the
    check stricter — it makes it wrong in both directions at once. A lane that
    runs its ecosystem's own install step inside its worktree had the whole
    dependency tree counted as undeclared writes and lost the attempt to §7.3
    clause 4, while the delta it did earn was never affected either way.

    Untracked content stays in scope, and that is what conjunct (2) needs: a
    provisioned untracked file git would commit is measured, so tampering with
    it still convicts unconditionally. Only what git itself excludes is out,
    which is why this is a statement about membership rather than an allowlist
    — no name is permitted here, and no caller can name one.

    A path git lists but the tree does not hold — a tracked file deleted in the
    worktree — is absent from the result, so `delta` sees it as removed. A path
    that is neither a regular file nor a symlink (a submodule's gitlink, a
    device node) is not committable content and carries no tuple.

    Every path in scope is hashed in full. A stat-based short-circuit is
    forbidden here (§8.3): an agent that rewrites a provisioned file and
    restores its stat metadata defeats a stat cache, which is the precise hole
    the content hash was chosen to close.
    """
    worktree = Path(worktree)
    regular: List[str] = []
    inv: Inventory = {}
    for rel in _committable_paths(worktree):
        full = worktree / rel
        if full.is_symlink():
            inv[rel] = (MODE_SYMLINK, _symlink_blob_id(worktree, rel))
        elif full.is_file():
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
        changed=tuple(
            sorted(k for k in after if k in before and after[k] != before[k])
        ),
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
    attempt.ignored_at_base = {
        rel: _disk_digest(attempt.path, rel)
        for rel in _files_absent_from_inventory(attempt.path, attempt.baseline)
    }
    return attempt.baseline


def expected_inventory(
    baseline: Inventory, measured: InventoryDelta, after: Inventory
) -> Inventory:
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
            raise WorktreeError(
                f"delta names {rel!r} but the after-inventory has no tuple"
            )
        expected[rel] = after[rel]
    for rel in measured.removed:
        expected.pop(rel, None)
    return expected


# ── §8.3 cache redirection ──────────────────────────────────────────────────

#: Configured lane count for this run. ``None`` means ``PYTEST_ADDOPTS``
#: carries only the cache redirect — ``-n`` requires pytest-xdist, and a
#: nested collection in a tree that does not install it must still probe.
_lane_concurrency: Optional[int] = None


def bind_lane_concurrency(concurrency: Optional[int]) -> None:
    """Record the run's lane count so gates inherit the worker cap.

    Unset (``None``) is the default and the test-isolation reset. A positive
    integer is the configured ``execution.concurrency``.
    """
    global _lane_concurrency
    if concurrency is not None and concurrency < 1:
        raise ValueError("concurrency is ≥ 1")
    _lane_concurrency = concurrency


def scratch_env(
    scratch: Path,
    *,
    concurrency: Optional[int] = None,
    cpu_count: Optional[int] = None,
) -> Dict[str, str]:
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

    Two of those three contexts are ordinary subprocesses of this harness and
    receive the mapping by inheritance. The pane is not: it is forked by the
    herdr server, and reaches it only through the `--env` flags
    `launcher.pane_env_flags` builds from `launcher.SCRATCH_ENV_KEYS`. A
    variable added here and not there is redirected for the gates and ignored
    by the agent — the asymmetry that convicted a node on 2026-08-17 — so the
    two sets are asserted equal on every call rather than left to review.

    ``PYTEST_ADDOPTS`` carries the per-lane xdist cap only when a lane
    count is known. Unspecified concurrency keeps the cache redirect alone,
    because ``-n`` is an xdist flag and a tree without that plugin must
    still collect.

    """
    scratch = Path(scratch)
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    if cores < 1:
        cores = 1
    lanes = concurrency if concurrency is not None else _lane_concurrency
    cache_dir = scratch / "pytest_cache"
    if lanes is None:
        addopts = "-o cache_dir={}".format(cache_dir)
    else:
        addopts = "-n {} -o cache_dir={}".format(
            pytest_worker_cap(lanes, cores), cache_dir
        )
    values = {
        # `XDG_CACHE_HOME` was here until 2026-08-27. It names a directory
        # tools read as well as write, and a pane's login shell reads its
        # credentials through it, so redirecting it removed the shell's own
        # secret bootstrap and launched agents with no usable model. See
        # `launcher.SCRATCH_ENV_KEYS`, which this set is asserted equal to
        # below -- the two move together or not at all.
        "TMPDIR": str(scratch / "tmp"),
        "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
        "PYTEST_ADDOPTS": addopts,
        "COVERAGE_FILE": str(scratch / "coverage"),
        "RUFF_CACHE_DIR": str(scratch / "ruff"),
        "npm_config_cache": str(scratch / "npm"),
    }
    unforwarded = sorted(set(values) - set(SCRATCH_ENV_KEYS))
    if unforwarded:
        raise WorktreeError(
            "SCRATCH_ENV_NOT_FORWARDED_TO_PANE:{}".format(",".join(unforwarded))
        )
    unbuilt = sorted(set(SCRATCH_ENV_KEYS) - set(values))
    if unbuilt:
        raise WorktreeError("SCRATCH_ENV_NOT_BUILT:{}".format(",".join(unbuilt)))
    return values


def launch_env(
    scratch: Path,
    base: Optional[Mapping[str, str]] = None,
    *,
    concurrency: Optional[int] = None,
    cpu_count: Optional[int] = None,
) -> Dict[str, str]:
    """A complete environment for anything launched inside the bracket.

    The scratch subdirectories are created here because a redirection to a
    directory that does not exist is a redirection some tools ignore, and an
    ignored redirection is indistinguishable from no redirection until it
    convicts someone.
    """
    env = dict(os.environ if base is None else base)
    redirected = scratch_env(scratch, concurrency=concurrency, cpu_count=cpu_count)
    for key in ("TMPDIR", "PYTHONPYCACHEPREFIX", "npm_config_cache"):
        Path(redirected[key]).mkdir(parents=True, exist_ok=True)
    cache_dir = redirected["PYTEST_ADDOPTS"].split("cache_dir=", 1)[1].split()[0]
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
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
    return any(
        relpath == pattern or fnmatch.fnmatchcase(relpath, pattern)
        for pattern in declared
    )


@dataclass(frozen=True)
class PermissionVerdict:
    """§8.3's two conjuncts, both computed from measured state."""

    passes: bool
    conjunct1_violations: Tuple[str, ...] = ()
    conjunct2_violations: Tuple[str, ...] = ()


def permission_check(
    attempt: AttemptWorktree, measured: InventoryDelta, declared: Sequence[str]
) -> PermissionVerdict:
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
        raise WorktreeError(
            "permission_check before take_baseline — the bracket never opened"
        )

    c1 = tuple(rel for rel in measured.touched if not _matches_any(rel, declared))
    c2: List[str] = []
    for rel in measured.changed + measured.removed:
        if rel not in attempt.tracked_at_base:
            c2.append(f"modified or deleted but untracked at the attempt's base: {rel}")
    for rel in measured.added:
        if rel in attempt.baseline:
            c2.append(f"added but already present in the baseline inventory: {rel}")
    return PermissionVerdict(
        passes=not c1 and not c2,
        conjunct1_violations=c1,
        conjunct2_violations=tuple(c2),
    )


# ── a declared output git will not commit ───────────────────────────────────


def _disk_digest(worktree: Path, rel: str) -> str:
    """Content identity of one on-disk path, or 'missing' if it cannot be read."""
    path = Path(worktree) / rel
    try:
        if path.is_symlink():
            return "symlink:" + os.readlink(path)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _files_absent_from_inventory(worktree: Path, present: Inventory) -> Tuple[str, ...]:
    """Regular files on disk that `inventory()` cannot see.

    Inventory's universe is `git ls-files --cached --others --exclude-standard`,
    so gitignored writes are invisible to the delta. This walk is how the
    harness names those files without reimplementing exclude rules.
    """
    root = Path(worktree)
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".git" or rel_dir.startswith(".git" + os.sep):
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            rel = rel.replace(os.sep, "/")
            if rel not in present:
                found.append(rel)
    return tuple(found)


def existing_ignored_outputs(
    worktree: Path,
    declared: Sequence[str],
    after: Inventory,
    before: Optional[Mapping[str, str]],
) -> Tuple[str, ...]:
    """Declared outputs the attempt created or changed that git will not commit.

    Inventory's universe is `git ls-files --cached --others --exclude-standard`,
    so a gitignored write is invisible to the delta, the permission check, and
    the commit. A node that declared such a path then produces an empty commit
    while the file sits on disk — a silent success. This is the detector for
    that shape: the path matches a declared glob, is absent from the
    after-inventory, and either was not on disk at `take_baseline` or its
    bytes changed since then. Unchanged provision files stay silent, which is
    what keeps a provisioned `.venv` from convicting a node that declared
    `*.py` and wrote one perfectly committable file.

    `before` is required and may not be `None`, for the same reason
    `permission_check` refuses a `None` baseline: `reopen_attempt_worktree`
    hands back a handle whose before-side was never recorded, and reading a
    missing before-side as an empty one would attribute the whole provisioned
    ignored tree to the node. A caller without the recorded map has no
    before-side, and must say so rather than be given a wrong one.
    """
    if before is None:
        raise WorktreeError(
            "existing_ignored_outputs without the ignored-at-base map — the "
            "bracket never opened, and an empty before-side reads a provisioned "
            "dependency tree as the node's own writes"
        )
    root = Path(worktree)
    prior = dict(before)
    after_ignored = {
        rel: _disk_digest(root, rel)
        for rel in _files_absent_from_inventory(root, after)
    }
    ignored: List[str] = []
    for rel, digest in after_ignored.items():
        if not _matches_any(rel, declared):
            continue
        if rel not in prior or prior[rel] != digest:
            ignored.append(rel)
    return tuple(sorted(ignored))


def outputs_ignored_in_repo(repo: Path, declared: Sequence[str]) -> Tuple[str, ...]:
    """Concrete declared outputs `git check-ignore` would exclude at `repo`.

    Globs are skipped: check-ignore names paths, not patterns. A glob that
    happens to match ignored files is caught at attempt settle by
    `existing_ignored_outputs`.
    """
    concrete = [path for path in declared if not _is_glob(path)]
    if not concrete:
        return ()
    result = _git(
        repo, "check-ignore", "--stdin", check=False, stdin="\n".join(concrete) + "\n"
    )
    if result.returncode not in (0, 1):
        raise WorktreeError(
            f"git check-ignore failed in {repo}: {(result.stderr or result.stdout).strip()}"
        )
    return tuple(
        line.replace(os.sep, "/") for line in result.stdout.splitlines() if line
    )


def _is_glob(pattern: str) -> bool:
    return any(char in pattern for char in "*?[]")


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
    result = _git(
        attempt.repo, "update-ref", attempt.ref, output_sha, attempt.base, check=False
    )
    if result.returncode != 0:
        raise CompareAndSwapRefused(
            f"{attempt.ref} no longer at the recorded base {attempt.base[:10]}: "
            f"{result.stderr.strip()}"
        )


def prepare_descendant_candidate(attempt: AttemptWorktree, parent: str) -> None:
    """Open the next measured bracket atop an already-published candidate.

    The attempt branch remains the durable identity through every candidate
    cycle.  This only moves the mutable measurement base after proving both
    refs already name the requested immutable parent; it never creates a
    worktree, branch, scratch directory, or commit.
    """
    if not is_object_digest(parent):
        raise InvalidCandidateParent(
            f"candidate parent must be an exact 40- or 64-hex digest, got {parent!r}"
        )
    resolved = _git(
        attempt.repo,
        "rev-parse",
        "--verify",
        "--quiet",
        f"{parent}^{{commit}}",
        check=False,
    )
    if resolved.returncode != 0 or resolved.stdout.strip() != parent:
        raise InvalidCandidateParent(
            f"candidate parent {parent} does not resolve as a commit in {attempt.repo}"
        )

    head = _out(attempt.path, "rev-parse", "HEAD")
    if head != parent:
        raise HeadMoved(
            f"HEAD is {head[:10]}, not candidate parent {parent[:10]} for the next cycle"
        )
    ref_parent = attempt_ref_commit(
        attempt.repo, attempt.run_id, attempt.node_id, attempt.attempt_no
    )
    if ref_parent != parent:
        raise CompareAndSwapRefused(
            f"{attempt.ref} is {ref_parent or 'absent'}, not candidate parent {parent[:10]}"
        )

    tracked_at_parent = frozenset(inventory_at_commit(attempt.repo, parent))
    attempt.base = parent
    attempt.tracked_at_base = tracked_at_parent
    attempt.baseline = None
    attempt.ignored_at_base = None


def _recover_candidate_baseline(
    attempt: AttemptWorktree,
    parent: str,
    original_base: str,
    original_baseline: Inventory,
) -> Inventory:
    """Rebuild a repair bracket without trusting mutable worktree bytes."""
    original_tracked = inventory_at_commit(attempt.repo, original_base)
    parent_tree = inventory_at_commit(attempt.repo, parent)
    baseline = {
        path: entry
        for path, entry in original_baseline.items()
        if path not in original_tracked and path not in parent_tree
    }
    baseline.update(parent_tree)
    attempt.base = parent
    attempt.tracked_at_base = frozenset(parent_tree)
    attempt.baseline = baseline
    return baseline


def recover_unsealed_descendant(
    attempt: AttemptWorktree,
    parent: str,
    original_baseline: Inventory,
) -> Inventory:
    """Recover a repair bracket before its descendant commit is sealed."""
    original_base = attempt.base
    prepare_descendant_candidate(attempt, parent)
    return _recover_candidate_baseline(
        attempt, parent, original_base, original_baseline
    )


def sealed_descendant_tip(attempt: AttemptWorktree, parent: str) -> str:
    """HEAD if it is this attempt's merge-free descendant of ``parent``.

    The retained builder may add follow-up commits in the same declared
    generation. The attempt ref and a merge-free parent-to-tip ancestry
    prove which candidate cycle produced that lineage. Raises ``HeadMoved``
    when HEAD/ref is not that descendant, including when they still name
    ``parent`` itself.
    """
    tip = attempt_ref_commit(
        attempt.repo, attempt.run_id, attempt.node_id, attempt.attempt_no
    )
    head = _out(attempt.path, "rev-parse", "HEAD")
    if tip is None or tip != head:
        raise HeadMoved(
            f"attempt ref is {tip or 'absent'}, but worktree HEAD is {head}"
        )
    ancestor = _git(
        attempt.repo, "merge-base", "--is-ancestor", parent, tip, check=False
    )
    merges = _out(attempt.repo, "rev-list", "--merges", f"{parent}..{tip}")
    if tip == parent or ancestor.returncode != 0 or merges:
        raise HeadMoved(
            f"sealed repair {tip[:10]} is not a linear descendant of "
            f"candidate {parent[:10]}"
        )
    return tip


def recover_sealed_descendant(
    attempt: AttemptWorktree,
    parent: str,
    original_baseline: Inventory,
) -> Tuple[Inventory, str]:
    """Recover one quiesced repair lineage after its published parent.

    The private-index commit can survive a scheduler crash before
    ``record_sealed_output`` advances the attempt ledger. The retained builder
    may then add follow-up commits in the same declared generation before the
    harness consumes its final envelope. The attempt ref and a merge-free
    parent-to-tip ancestry prove which candidate cycle produced that lineage.
    The original provisioned baseline supplies the untracked paths that git
    cannot retain; the published parent tree supplies the tracked side of the
    repair bracket.
    """
    tip = sealed_descendant_tip(attempt, parent)

    original_base = attempt.base
    baseline = _recover_candidate_baseline(
        attempt, parent, original_base, original_baseline
    )
    return baseline, tip


def commit_measured_delta(
    attempt: AttemptWorktree, measured: InventoryDelta, after: Inventory, message: str
) -> str:
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
    node has an output SHA and the merge guard stays uniform (§8.4). A
    *repair* attempt with an empty delta is classified before this function
    is called, so it never mints a new sha for a byte-identical tree (#113).
    """
    if attempt.baseline is None:
        raise WorktreeError("commit_measured_delta before take_baseline")
    if _is_inside(attempt.private_index, attempt.path) or _is_inside(
        attempt.private_index, attempt.scratch
    ):
        raise WorktreeError(
            f"the private index {attempt.private_index} is inside the worktree or the "
            "shared scratch, which would make the index itself a delta path (§8.4)"
        )

    head = _out(attempt.path, "rev-parse", "HEAD")
    if head != attempt.base:
        raise HeadMoved(
            f"HEAD is {head[:10]}, not the attempt's recorded base {attempt.base[:10]} — "
            "something committed in this worktree that was not the harness"
        )

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
                    "something wrote to the tree between the after-inventory and staging"
                )
        for rel in measured.removed:
            if rel in staged:
                raise StagingMismatch(
                    f"{rel} was measured as deleted but is still staged"
                )

        tree = _out(attempt.path, "write-tree", env=env)
        output_sha = _out(
            attempt.path,
            "commit-tree",
            tree,
            "-p",
            attempt.base,
            "-m",
            message,
            env=env,
        )
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


# ── §7.4's post-work falsification: take the subject back out ───────────────


def _tracked_at(worktree: Path, sha: str, relpath: str) -> bool:
    """Is `relpath` in the tree of `sha`?"""
    return (
        _git(
            worktree, "cat-file", "-e", "{0}:{1}".format(sha, relpath), check=False
        ).returncode
        == 0
    )


def paths_written_since(attempt: AttemptWorktree, base: str) -> Tuple[str, ...]:
    """Paths this node added, modified, copied or renamed between `base` and HEAD.

    HEAD is the sealed output commit. `base` is the node's **falsifiability**
    base — the integration head the chain root branched from — which is
    `attempt.base` for an ordinary attempt and the chain root's head for a
    repair (§7.4). The distinction is the whole reason this reads git rather
    than the attempt's own measured delta: a repair attempt that edits only its
    test file has a measured delta of one path, and asking the falsification
    question over that delta would find nothing to revert and pass vacuously,
    which is the exact escape the check exists to close.

    Deletions are excluded. Restoring a file the node deleted cannot make its
    gate go red, so a deleted path is not a subject this question has.
    """
    listed = _out(
        attempt.path, "diff", "--name-only", "--diff-filter=ACMR", base, "HEAD"
    )
    return tuple(line for line in listed.splitlines() if line.strip())


def revert_paths_to(
    attempt: AttemptWorktree, base: str, relpaths: Sequence[str]
) -> Tuple[str, ...]:
    """Put `relpaths` back to their content at `base`.

    Called only after `commit_measured_delta` has sealed the output commit, so
    nothing done here can reach the integration branch: the merge consumes the
    committed object and this touches the working tree the commit was taken
    from. That is the same argument §8.4 already makes about running the
    post-node gate after the commit rather than before it.

    A path `base` does not hold is removed rather than checked out — it is new
    in this node's work, and "revert" for a new file is its absence. The
    removal is `os.remove` and not a shell `rm`, because this module never
    shells out to anything but git.

    Returns the paths actually reverted, which is what the caller records.
    """
    reverted: List[str] = []
    for rel in relpaths:
        if _tracked_at(attempt.path, base, rel):
            _git(attempt.path, "checkout", base, "--", rel)
        else:
            target = attempt.path / rel
            if target.is_symlink() or target.exists():
                os.remove(target)
            _git(attempt.path, "rm", "--cached", "-q", "--", rel, check=False)
        reverted.append(rel)
    return tuple(reverted)


def restore_paths_from_head(attempt: AttemptWorktree, relpaths: Sequence[str]) -> None:
    """Undo `revert_paths_to`, from the attempt's own output commit.

    HEAD is the sealed output commit by the time this runs — `commit_measured_
    delta` advanced the ref the worktree is checked out on — so restoring from
    it returns index and working tree to exactly the state the commit records.

    Correctness of the restore is not asserted here and deliberately gets no
    check of its own. Nothing downstream reads this tree: the merge consumes
    the committed object, so a path left reverted would be a stale worktree
    about to be removed, not a wrong merge. A checker here would be a check on
    a check with no failure to catch.
    """
    for rel in relpaths:
        _git(attempt.path, "checkout", "HEAD", "--", rel, check=False)


# ── §8.3 the four checks ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Divergence:
    """One path where the measured tree differs from the expected inventory."""

    path: str
    kind: str  # "unexpected", "missing", or "changed"
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
    #: Worktrees git has registered against this repository that no Maestro
    #: bracket provisioned — one report line per tree, path first. Populated
    #: only at the pre-merge evaluation, because that is the point at which an
    #: agent has finished writing and a tree it created and abandoned is
    #: observable. Report-only by construction: see `unprovisioned_worktrees`.
    unprovisioned_worktrees: Tuple[str, ...] = ()


def compare_to_expected(
    worktree: Path, expected: Inventory, consequence: str
) -> CleanlinessVerdict:
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
    return CleanlinessVerdict(
        clean=not divergences, consequence=consequence, divergences=tuple(divergences)
    )


def _git_checks(attempt: AttemptWorktree) -> Tuple[bool, bool, bool, Tuple[str, ...]]:
    """The three git-side checks: branch checked out, HEAD resolves, ancestry."""
    detail: List[str] = []
    branch = _git(attempt.path, "symbolic-ref", "--short", "HEAD", check=False)
    branch_checked_out = (
        branch.returncode == 0 and branch.stdout.strip() == attempt.branch
    )
    if not branch_checked_out:
        detail.append(
            f"HEAD is not on {attempt.branch}: {branch.stdout.strip() or 'detached'}"
        )

    head = _git(
        attempt.path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False
    )
    head_resolves = head.returncode == 0
    if not head_resolves:
        detail.append("HEAD does not resolve to a commit")

    base_is_ancestor = False
    if head_resolves:
        base_is_ancestor = (
            _git(
                attempt.path,
                "merge-base",
                "--is-ancestor",
                attempt.base,
                "HEAD",
                check=False,
            ).returncode
            == 0
        )
        if not base_is_ancestor:
            detail.append(
                f"the recorded base {attempt.base[:10]} is not an ancestor of HEAD"
            )
    return branch_checked_out, head_resolves, base_is_ancestor, tuple(detail)


def _registered_worktrees(repo: Path) -> Tuple[Tuple[Path, Optional[str]], ...]:
    """Every worktree git has registered against `repo`, with its branch.

    git's own registration table is the authority here, and deliberately so.
    The alternative — walking `/tmp` for directories that look like worktrees —
    is slower, misses every tree created anywhere else, and cannot tell a
    registered worktree from an abandoned copy of one. `git worktree list
    --porcelain` answers the only question that matters: which trees does this
    repository currently believe it has.

    A second element of `None` means the tree has a detached HEAD, which is
    what `git worktree add --detach` produces and what every observed
    reviewer-created tree has had.
    """
    result = _git(repo, "worktree", "list", "--porcelain", check=False)
    if result.returncode != 0:
        return ()
    registrations: List[Tuple[Path, Optional[str]]] = []
    path: Optional[Path] = None
    branch: Optional[str] = None
    bare = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
            branch, bare = None, False
        elif line.startswith("branch "):
            branch = line[len("branch ") :].strip()
        elif line.strip() == "bare":
            bare = True
        elif not line.strip() and path is not None:
            if not bare:
                registrations.append((path, branch))
            path, branch, bare = None, None, False
    if path is not None and not bare:
        registrations.append((path, branch))
    return tuple(registrations)


def _resolved(path: Path) -> Path:
    """Resolve for comparison, tolerating a registration whose tree is gone."""
    try:
        return path.resolve()
    except OSError:
        return path


def unprovisioned_worktrees(attempt: AttemptWorktree) -> Tuple[str, ...]:
    """Worktrees registered against this repository that Maestro did not create.

    Reviewer agents create ad-hoc detached worktrees of their own accord — one
    transcript ran `git worktree add --detach /tmp/lexgenius-review-<sha>`
    twelve times with no matching `worktree remove` — and no Maestro code
    creates or removes them. Those trees are outside the attempt worktree
    entirely, so `compare_to_expected` cannot see them: it measures one tree's
    inventory, and residue that lives in a *different* tree is invisible to it
    however carefully the inventory is compared.

    The discriminator is **detachment**, and it is a structural property of
    Maestro's own code rather than a guess about who owns a path. Every
    worktree this system registers against a run's repository is created on a
    branch: `create_worktree` above opens each attempt with `git worktree add
    -b`, and `run start` opens the run's integration checkout the same way.
    The one `--detach` call Maestro makes anywhere — the deterministic
    cross-repository acceptance checkout — runs at final acceptance, after
    every node has merged, so no attempt is at its pre-merge evaluation while
    one exists. A detached worktree observed here is therefore not one of
    ours, whatever it is called and wherever it lives.

    Naming a path pattern instead would have been the wrong instrument twice
    over: `/tmp` is where the observed reviewers happened to write, not where
    the next one must, and an operator's own checkout of the repository sits
    outside every Maestro root while being nobody's residue. Detachment
    separates the two without the code having to recognise either.

    The two path exemptions below are belt-and-braces for a Maestro tree that
    somehow arrives detached — the main worktree the attempt branches from,
    and anything under the worktrees root `create_attempt_worktree` puts every
    attempt of every node in, so a sibling or a concurrent run is never
    reported.

    A leak on a branch is not reported and that is a stated limit, not an
    oversight: it is a false negative on a detection-only signal, where the
    alternative is reporting every developer checkout of the repository as
    residue and teaching the operator to ignore the channel.
    """
    provisioned_root = _resolved(attempt.path.parent)
    main = _resolved(attempt.repo)
    lines: List[str] = []
    for path, branch in _registered_worktrees(attempt.path):
        if branch:
            continue
        resolved = _resolved(path)
        if resolved == main:
            continue
        if resolved == provisioned_root or provisioned_root in resolved.parents:
            continue
        lines.append("{0} (detached)".format(resolved))
    return tuple(sorted(lines))


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
    return CheckResult(
        "post-commit", branch_ok, head_ok, ancestor_ok, cleanliness, ok, ok, detail
    )


def check_pre_merge(attempt: AttemptWorktree, expected: Inventory) -> CheckResult:
    """After the post-node gate has run, immediately before the merge (§8.3).

    Residue here is **reported** as an adapter hygiene defect with the paths
    named rather than convicting the node: the commit was sealed before the
    gate ran and the merge consumes committed objects, so a post-gate rewrite
    is a maintenance signal about the adapter and never a merge hazard.

    Residue outside the attempt's own tree is reported on the same terms and
    for a stronger reason. A worktree an agent registered against the
    repository and abandoned is real residue — it holds a checkout and an
    administrative entry under `.git/worktrees` that nothing will reclaim —
    but it is not the producing node's doing, and `merge_permitted` therefore
    does not move. Blocking the merge would strand a node's correct, gated,
    committed work on a reviewer's housekeeping, which is the shape §8.3
    already refuses for in-tree post-gate residue and refuses more plainly
    here, where the node did not even perform the act being reported.
    """
    branch_ok, head_ok, ancestor_ok, detail = _git_checks(attempt)
    cleanliness = compare_to_expected(attempt.path, expected, "report")
    ok = branch_ok and head_ok and ancestor_ok and cleanliness.clean
    return CheckResult(
        "pre-merge",
        branch_ok,
        head_ok,
        ancestor_ok,
        cleanliness,
        ok,
        True,
        detail,
        unprovisioned_worktrees(attempt),
    )


# ── gates: node-scoped by default, whole-suite only as integration ──────────

_COUNT = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped)")


@dataclass(frozen=True)
class GateResult:
    """One gate execution. §10.2's counting rule adjudicates a parsed report;
    what this records is the execution itself — command, scope, exit code."""

    label: str
    scope: str  # "node" or "integration"
    selector: Optional[str]
    command: Tuple[str, ...]
    exit_code: int
    green: bool
    counts: Dict[str, int] = field(default_factory=dict)
    tail: Tuple[str, ...] = ()


def _run_gate(
    worktree: Path,
    runner: "rr.ResolvedRunner",
    argv: Sequence[str],
    scratch: Path,
    label: str,
    scope: str,
    selector: Optional[str],
    cancel_requested: Callable[[], bool],
) -> GateResult:
    """Run one gate through a resolved runner.

    `runner` is a `ResolvedRunner` and not a command, and that is the whole
    repair: this function used to take `Sequence[str]` whose `argv[0]` was the
    bare `Gate.runner` literal, executed with an inherited `PATH`. A wrong
    interpreter then produced no summary line, `counts` came back empty,
    `GateCounts.parse` returned `None`, `adjudicate_gate` stamped
    `ENVIRONMENTAL`, and the node re-ran the same broken binary until its
    environmental budget was gone. Taking a resolved runner means a caller has
    nothing to build an invocation from except something `runner_resolution.
    resolve` produced and probed, so the wrong-interpreter case is refused at
    run start instead of misclassified at attempt time (§1.2).
    """
    if not isinstance(runner, rr.ResolvedRunner):
        raise TypeError(
            "a gate runs through a resolved runner, never a bare command; "
            "obtain one from runner_resolution.resolve"
        )
    command = runner.execute_argv(argv)
    try:
        result = run_harness_process(
            command,
            cwd=worktree,
            env=launch_env(scratch),
            cancel_requested=cancel_requested,
        )
    except HarnessCancelled as exc:
        raise GateCancelled("gate cancelled before a result was produced") from exc
    output = (result.stdout or "") + (result.stderr or "")
    counts = {
        ("error" if key.startswith("error") else key): int(value)
        for value, key in _COUNT.findall(output)
    }
    return GateResult(
        label=label,
        scope=scope,
        selector=selector,
        command=tuple(command),
        exit_code=result.returncode,
        green=result.returncode == 0,
        counts=counts,
        tail=tuple(output.strip().splitlines()[-5:]),
    )


def run_node_gate(
    attempt: AttemptWorktree,
    runner: "rr.ResolvedRunner",
    argv: Sequence[str],
    selector: str,
    cancel_requested: Callable[[], bool],
    label: str = "node-gate",
) -> GateResult:
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
            "run_integration_gate (§8.8), never an unscoped node gate"
        )
    # The selector is appended token by token, and only where `argv` does not
    # already carry it. Appended whole, as one argv element, it worked for a
    # single-path selector by accident -- the caller in `maestro` passes the
    # gate's own argv, which already contains the selector, and the runner
    # deduplicated the repeat. A two-path selector appended the same way is
    # one argv element reading "tests/a.py tests/b.py", a path that does not
    # exist, and the gate exits 4 having collected nothing. Same shape as the
    # validator's `all(path in produced ...)`: a selector is a set of paths,
    # and treating it as one opaque string is only ever right for one path.
    extra = [token for token in str(selector).split() if token not in argv]
    return _run_gate(
        attempt.path,
        runner,
        list(argv) + extra,
        attempt.scratch,
        label,
        "node",
        selector,
        cancel_requested,
    )


def run_integration_gate(
    worktree: Path,
    runner: "rr.ResolvedRunner",
    argv: Sequence[str],
    scratch: Path,
    cancel_requested: Callable[[], bool],
    label: str = "integration-gate",
) -> GateResult:
    """Run the final whole-suite gate under the caller's cancellation lease.

    This is the only gate deliberately unscoped: it judges the integrated tree,
    where semantic conflicts between individually-correct nodes become visible.
    """
    return _run_gate(
        Path(worktree),
        runner,
        argv,
        Path(scratch),
        label,
        "integration",
        None,
        cancel_requested,
    )


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
    eligible = [
        n
        for n in records
        if n.state != "MERGED"
        and n.state not in TERMINAL_WITHOUT_MERGE
        and all(dep in merged for dep in n.needs)
    ]
    return tuple(
        n.node_id for n in sorted(eligible, key=lambda n: (n.depth, n.node_id))
    )


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


def merge_verified_node(
    integration_path: Path, node_id: str, output_sha: str, message: Optional[str] = None
) -> MergeResult:
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
    merge = _git(
        integration_path,
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        message or f"merge {node_id}",
        output_sha,
        check=False,
    )

    if merge.returncode != 0:
        conflicted = tuple(
            sorted(
                line
                for line in _out(
                    integration_path, "diff", "--name-only", "--diff-filter=U"
                ).splitlines()
                if line
            )
        )
        _git(integration_path, "merge", "--abort", check=False)
        head_after = _out(integration_path, "rev-parse", "HEAD")
        if head_after != head_before:
            raise WorktreeError(
                f"aborting the merge of {node_id} left the integration head at "
                f"{head_after[:10]} instead of {head_before[:10]}"
            )
        return MergeResult(
            node_id=node_id,
            output_sha=output_sha,
            head_before=head_before,
            head_after=head_after,
            conflicted_paths=conflicted,
            detail=merge.stderr.strip() or merge.stdout.strip(),
        )

    ancestry = (
        _git(
            integration_path,
            "merge-base",
            "--is-ancestor",
            output_sha,
            "HEAD",
            check=False,
        ).returncode
        == 0
    )
    head_after = _out(integration_path, "rev-parse", "HEAD")
    return MergeResult(
        node_id=node_id,
        output_sha=output_sha,
        head_before=head_before,
        head_after=head_after,
        merged_sha=head_after if ancestry else None,
        ancestry_proven=ancestry,
    )


# ── §8.8 acceptance and cleanup ─────────────────────────────────────────────


def final_ancestry_sweep(
    integration_path: Path, output_shas: Mapping[str, str]
) -> Dict[str, bool]:
    """Re-prove every merged node against the **final** head (§8.6).

    A run is not accepted otherwise, regardless of green tests: test PASS is
    structurally never merge provenance, so the only evidence that a node's
    content is in the final tree is git's own ancestry answer at the end.
    """
    integration_path = Path(integration_path)
    return {
        node_id: _git(
            integration_path, "merge-base", "--is-ancestor", sha, "HEAD", check=False
        ).returncode
        == 0
        for node_id, sha in output_shas.items()
    }


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


def remove_attempt_worktree(
    attempt: AttemptWorktree,
    ancestry_proven: bool,
    integration_path: Optional[Path] = None,
) -> None:
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
            "an unmerged branch destroys the only copy of the work (§8.8)"
        )
    _git(attempt.repo, "worktree", "remove", str(attempt.path))
    _git(
        Path(integration_path) if integration_path else attempt.repo,
        "branch",
        "-d",
        attempt.branch,
    )
