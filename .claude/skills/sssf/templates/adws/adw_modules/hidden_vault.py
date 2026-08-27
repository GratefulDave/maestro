"""Where a hidden tests node's bytes live, and why it is not the run repository.

A linked git worktree shares its parent repository's object database. That is
not a detail: it is the whole reason this module exists. `git rev-parse
--git-common-dir` inside an attempt worktree resolves to the *run repository's*
`.git`, so every object any attempt ever committed is readable from every other
attempt's worktree by one plumbing command:

    git -C <builder worktree> cat-file -p <blob of the tests node's file>

Executed 2026-08-27 against a real builder worktree, that command printed the
assertions the builder was being judged by. Withholding the tests from the
builder's *prompt* and from the *integration branch* does not close it, because
neither is how the bytes are reachable — the shared object database is.

So containment is a property of the object graph or it does not exist. A hidden
tests node's attempt is created from a **separate bare repository** — the vault
— under harness-private state. Objects flow run repository -> vault, by fetching
the integration branch the tests node must branch from, and never the other way.
The run repository therefore never receives the hidden bytes at all, and there is
nothing for a builder worktree to reach.

What this does not claim: §16.3 item 15 records that Maestro has no filesystem
sandbox, so an agent that writes or reads by absolute path is outside every
boundary here. The vault is unreachable from the builder's *repository*, not from
its *process*. Hidden tests are hidden from a cooperative-but-optimizing builder,
not from an adversarial one, and that limit is stated in
`docs/hidden-tests-design.md` §9 rather than papered over.

**Nothing in production calls this module yet, and that is deliberate rather
than an oversight — B15 asks for the reason to be written down, so here it is.**
This is the prerequisite half of `test_visibility: hidden`, landed on its own
because the design's verdict was that the feature is not worth building until
containment is proven (`docs/hidden-tests-design.md` §9, regression 7). The half
that would call it — the composed evaluation tree, the absence/provenance/
coverage conjuncts, the sanitised repair handoff — is not built, so
`maestro-plan.v5` is deliberately absent from
`maestro._RUNNABLE_PLAN_SCHEMA_VERSIONS` and no run can reach a hidden node at
all. The caller arrives in the same change that makes v5 runnable; until then
`tests/test_hidden_test_containment.py` is the only consumer, and it exercises
this module against real git rather than a fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal, Sequence

#: A tests lane's `test_visibility`. "merged" is every plan shipped before this
#: existed and stays the default forever (§6.3 freezes a shipped class).
Visibility = Literal["merged", "hidden"]

MERGED: Visibility = "merged"
HIDDEN: Visibility = "hidden"


class VaultError(RuntimeError):
    """The vault could not be created, seeded, or read."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise VaultError(
            "git {0} in {1} exited {2}: {3}".format(
                " ".join(args), cwd, result.returncode, result.stderr.strip()
            )
        )
    return result


def vault_path(state_root: Path, run_id: str) -> Path:
    """Where this run's vault lives — harness-private state, never the repo.

    `state_root` is the *installation's* state root, the parent of `runs/`, so
    the vault is not inside the run directory and therefore not a `..` traversal
    from a builder's own cwd. That placement buys nothing against an adversary —
    §16.3 item 15 records that no filesystem sandbox exists — but this module's
    job is to make the bytes unreachable through the builder's *repository*, and
    leaving them a sibling of its worktree would be an invitation rather than a
    boundary.

    Keyed by `run_id` because two runs of one installation must not share a
    vault: their tests nodes are different nodes with different acceptance, and
    a shared object database would put one run's hidden bytes in the other's
    reach for no reason.
    """
    return Path(state_root).resolve() / "vaults" / "{0}.git".format(run_id)


def ensure_vault(state_root: Path, run_id: str) -> Path:
    """The run's bare vault repository, created if absent. Idempotent.

    Bare because nothing checks anything out here: the vault holds objects and
    refs, and `git worktree add` serves the tests node from it.
    """
    path = vault_path(state_root, run_id)
    if (path / "HEAD").is_file():
        return path
    path.mkdir(parents=True, exist_ok=True)
    _git(path.parent, "init", "-q", "--bare", str(path))
    return path


def seed(vault: Path, repo: Path, branch: str) -> str:
    """Fetch `branch` from the run repository into the vault, one way.

    This is the only direction objects ever move. The tests node must branch
    from the integration head like any other node (§8.1), so the vault needs
    that commit; it never needs anything the tests node itself produced, and
    the run repository must never receive it.

    Fetching the *branch* rather than a bare sha deliberately: fetching an
    unadvertised object depends on `uploadpack.allowAnySHA1InWant`, which is a
    property of the operator's git configuration rather than of this design.
    """
    _git(
        vault,
        "fetch",
        "--no-tags",
        str(Path(repo).resolve()),
        "+refs/heads/{0}:refs/heads/{0}".format(branch),
    )
    return _git(vault, "rev-parse", branch).stdout.strip()


def attempt_repository(
    *,
    repo: Path,
    state_root: Path,
    run_id: str,
    visibility: Visibility,
    integration_branch: str,
) -> Path:
    """The repository an attempt's worktree is created from.

    For everything except a hidden tests node this is the run repository, which
    is what every node has always used and what `merged` visibility keeps.

    For a hidden tests node it is the vault, and that substitution *is* the
    containment: the attempt's commit lands in the vault's object database, so
    the run repository — and therefore every worktree that shares its object
    database — never holds the bytes.
    """
    if visibility != HIDDEN:
        return Path(repo).resolve()
    vault = ensure_vault(state_root, run_id)
    seed(vault, repo, integration_branch)
    return vault


def blob_id_in(tree_or_repo: Path, commit: str, path: str) -> str | None:
    """The object id of `path` at `commit`, or None when it is not a blob there.

    A local reimplementation rather than an import of `tests_chain.blob_id_at`:
    that one raises `TestsGitReadFailed`, whose vocabulary belongs to the tests
    chain's refusals, and this module is consulted from the containment checks
    where a git read failure is a vault fact.
    """
    listed = _git(
        tree_or_repo, "ls-tree", "-z", "--full-tree", commit, "--", path,
        check=False,
    )
    if listed.returncode != 0:
        return None
    records = [record for record in listed.stdout.split("\x00") if record]
    if len(records) != 1:
        return None
    meta, _, _name = records[0].partition("\t")
    try:
        _mode, kind, object_id = meta.split(" ", 2)
    except ValueError:
        return None
    return object_id if kind == "blob" else None


def object_is_absent(repo: Path, object_id: str) -> bool:
    """Does `repo`'s object database genuinely not hold `object_id`?

    `cat-file -e` is the question asked directly, and it is asked of the
    repository rather than of a worktree path on purpose: a worktree answers
    for its shared object database, which is exactly the equivalence this
    module exists to exploit when checking, and to break when storing.
    """
    return _git(repo, "cat-file", "-e", object_id, check=False).returncode != 0


def unreachable_from(worktree: Path, object_ids: Sequence[str]) -> bool:
    """No object in `object_ids` is readable from `worktree`.

    The assertion regression 7 makes, phrased so a caller cannot accidentally
    check the wrong direction: it is true when containment holds.
    """
    for object_id in object_ids:
        if _git(worktree, "cat-file", "-e", object_id, check=False).returncode == 0:
            return False
    return True
