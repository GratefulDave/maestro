"""Private-test object isolation: a bare vault outside the run repository.

A linked Git worktree shares its parent object database. Private tests
therefore cannot be committed in the run or product repository: a builder
worktree would read them with `git cat-file`. Objects move only
run-repository -> vault. Drafts, seals, and private runner results live in
the vault and never return.
"""

from __future__ import annotations

import os
import shutil
import re
import stat
import subprocess
import tarfile
import threading
from io import BytesIO
from pathlib import Path
from typing import Iterable, Mapping, Sequence

_IDENTITY = re.compile(r"^[A-Za-z0-9._-]+$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HOOKS = "maestro-private-review-no-hooks"


class VaultError(RuntimeError):
    """The vault could not be created, seeded, or read."""


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
    input: bytes | str | None = None,
    text: bool | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    if text is None:
        text = not isinstance(input, (bytes, bytearray))
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=text,
        input=input,
        env=env,
    )
    if check and result.returncode != 0:
        err = result.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        raise VaultError(
            "git {0} in {1} exited {2}: {3}".format(
                " ".join(args), cwd, result.returncode, (err or "").strip()
            )
        )
    return result


def _require_id(name: str, value: str) -> str:
    if not _IDENTITY.fullmatch(value):
        raise VaultError("{0} {1!r} is not a vault identity".format(name, value))
    return value


def vault_path(state_root: Path, run_id: str) -> Path:
    """Harness-private bare repository for one run. Never the product repo."""
    _require_id("run_id", run_id)
    return Path(state_root).resolve() / "vaults" / "{0}.git".format(run_id)


_VAULT_LOCKS_GUARD = threading.Lock()
_VAULT_LOCKS: dict[str, threading.RLock] = {}


def _vault_lock(path: Path) -> threading.RLock:
    """One in-process lock per vault for its run-wide mutations.

    Lanes author concurrently, and each one ensures the run's vault and
    seeds the same `refs/maestro/integration-seed` before it collects. Both
    are idempotent in effect and neither is atomic: a second `git init` into
    a directory the first is still initialising fails on `config` already
    existing, and a second `git fetch` onto a ref the first is updating fails
    to lock it. The lock makes the second caller find the first one's result.
    """
    key = str(Path(path).resolve())
    with _VAULT_LOCKS_GUARD:
        lock = _VAULT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _VAULT_LOCKS[key] = lock
        return lock


def ensure_vault(state_root: Path, run_id: str) -> Path:
    """The run's bare vault, created if absent. Idempotent. Mode 0700."""
    path = vault_path(state_root, run_id)
    parent = path.parent
    with _vault_lock(path):
        if path.is_dir() and (path / "HEAD").is_file():
            return path
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _git(parent, "init", "-q", "--bare", str(path))
        os.chmod(path, stat.S_IRWXU)
        _git(path, "config", "core.hooksPath", _HOOKS)
        return path


def seed(vault: Path, repo: Path, integration_ref: str) -> str:
    """Fetch `integration_ref` from the run repository into the vault, one way."""
    if not integration_ref or integration_ref.startswith("-"):
        raise VaultError("integration_ref {0!r} is not a ref".format(integration_ref))
    dest = "refs/maestro/integration-seed"
    with _vault_lock(vault):
        _git(
            vault,
            "fetch",
            "--no-tags",
            str(Path(repo).resolve()),
            "+{0}:{1}".format(integration_ref, dest),
        )
        return _git(vault, "rev-parse", dest).stdout.strip()


def draft_ref(run_id: str, lane_id: str, private_draft_digest: str) -> str:
    """Name the ref that pins one private test draft.

    Keyed on the draft's own manifest digest -- the commit it pins and the
    private blobs that commit carries -- never on the review input that asked
    for it. A tester answers one input with whatever it writes, so an input
    can legitimately produce several drafts; each names its own ref, and a
    re-draft of identical bytes reaches the identical ref. See
    `commit_all` for why the commit sha is a function of those bytes.
    """
    return "refs/maestro/drafts/{0}/{1}/{2}".format(
        _require_id("run_id", run_id),
        _require_id("lane_id", lane_id),
        _require_digest("private_draft_digest", private_draft_digest),
    )


def sealed_ref(run_id: str, lane_id: str, sealed_digest: str) -> str:
    """Name the ref that pins one sealed test bundle.

    Keyed on `sealed_digest`, the manifest digest of the commit it pins, for
    the same reason as `draft_ref`.
    """
    return "refs/maestro/sealed/{0}/{1}/{2}".format(
        _require_id("run_id", run_id),
        _require_id("lane_id", lane_id),
        _require_digest("sealed_digest", sealed_digest),
    )


def private_results_ref(run_id: str, lane_id: str, results_digest: str) -> str:
    """Name the ref that pins one private test result.

    Keyed on the digest of the result bytes themselves, not on the review's
    input: the runner's output is an observation (its summary line carries a
    wall-clock duration), so one input can legitimately yield many results.
    Keying on the content makes `pin_object_ref` idempotent by construction.
    """
    return "refs/maestro/private-results/{0}/{1}/{2}".format(
        _require_id("run_id", run_id),
        _require_id("lane_id", lane_id),
        _require_digest("results_digest", results_digest),
    )


def _require_digest(name: str, value: str) -> str:
    # Named by the caller: each ref helper is keyed on a different digest, and
    # the refusal must say which one it was handed.
    if not _DIGEST.fullmatch(value):
        raise VaultError("{0} {1!r} is not SHA-256 hex".format(name, value))
    return value


def blob_id_in(tree_or_repo: Path, commit: str, path: str) -> str | None:
    listed = _git(
        tree_or_repo,
        "ls-tree",
        "-z",
        "--full-tree",
        commit,
        "--",
        path,
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
    return _git(repo, "cat-file", "-e", object_id, check=False).returncode != 0


def unreachable_from(worktree: Path, object_ids: Sequence[str]) -> bool:
    for object_id in object_ids:
        if _git(worktree, "cat-file", "-e", object_id, check=False).returncode == 0:
            return False
    return True


def rev_parse(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", rev).stdout.strip()


def advertised_object_ids(repo: Path) -> frozenset[str]:
    listed = _git(repo, "for-each-ref", "--format=%(objectname)")
    return frozenset(line for line in listed.stdout.split() if line)


def batch_object_ids(repo: Path) -> frozenset[str]:
    listed = _git(
        repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"
    )
    return frozenset(line for line in listed.stdout.split() if line)


def rev_list_objects(repo: Path) -> str:
    return _git(repo, "rev-list", "--all", "--objects").stdout


def prove_absent(repositories: Sequence[Path], object_ids: Sequence[str]) -> None:
    present = []
    for repo in repositories:
        for object_id in object_ids:
            if not object_is_absent(repo, object_id):
                present.append((str(Path(repo).resolve()), object_id))
    if present:
        raise VaultError(
            "private objects present outside the vault: {0}".format(present)
        )


def prove_unfetchable(source: Path, dest: Path, object_id: str) -> None:
    probe = subprocess.run(
        ["git", "fetch", str(Path(source).resolve()), object_id],
        cwd=str(dest),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if probe.returncode == 0:
        raise VaultError(
            "fetch of private object {0} from {1} succeeded".format(object_id, source)
        )
    if not object_is_absent(dest, object_id):
        raise VaultError("fetch left private object {0} in {1}".format(object_id, dest))


def checkout_vault_worktree(vault: Path, base_sha: str, dest: Path) -> Path:
    dest = Path(dest)
    if dest.exists():
        raise VaultError("refusing to adopt existing worktree {0}".format(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(vault, "worktree", "add", "--detach", str(dest), base_sha)
    _git(dest, "config", "core.hooksPath", _HOOKS)
    return dest.resolve()


def scratch_worktree_path(worktrees_root: Path, prefix: str) -> Path:
    """A path for a vault worktree that exists only while one call runs.

    The name carries no identity: two calls over the same input get two
    names, so a worktree left behind by a crashed call can never be the one a
    retry is refused for adopting. What the worktree produced lives in the
    vault object database; the directory is scaffolding and is removed by
    `remove_vault_worktree` when the call ends.
    """
    return Path(worktrees_root) / "{0}-{1}".format(prefix, os.urandom(8).hex())


def remove_vault_worktree(vault: Path, dest: Path) -> None:
    """Drop a scratch worktree and its registration; never raises."""
    dest = Path(dest)
    _git(vault, "worktree", "remove", "--force", str(dest), check=False)
    _git(vault, "worktree", "prune", check=False)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)


#: A vault commit is named by what it carries and nothing else.
#:
#: `git commit` embeds the wall clock in both the author and committer lines,
#: so two commits of one tree, one parent and one message made a second apart
#: have different shas. The drafts and sealed refs are keyed on a digest over
#: the commit they pin, so a sha that moved with the clock meant one input
#: could name two refs -- or, keyed the old way, one ref could be asked to pin
#: two shas, which `update_immutable_ref` correctly refuses. Fixing the dates
#: and identity, and switching signing off (a signature carries its own
#: timestamp), makes the sha a pure function of (tree, parent, message):
#: identical bytes commit to the identical sha, every time.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "maestro-private-review",
    "GIT_AUTHOR_EMAIL": "maestro-private-review@invalid",
    "GIT_AUTHOR_DATE": "@0 +0000",
    "GIT_COMMITTER_NAME": "maestro-private-review",
    "GIT_COMMITTER_EMAIL": "maestro-private-review@invalid",
    "GIT_COMMITTER_DATE": "@0 +0000",
}
_COMMIT_ARGS = ("-c", "commit.gpgsign=false", "commit", "-q")


def commit_all(
    worktree: Path, message: str, *, paths: Sequence[str] | None = None
) -> str:
    """Commit the worktree (or `paths`) and return a content-addressed sha.

    The sha depends only on the tree, the parent and `message`; see
    `_COMMIT_ENV`.
    """
    if paths is None:
        _git(worktree, "add", "-A")
        changed = bool(_git(worktree, "status", "--porcelain").stdout.strip())
        if not changed:
            raise VaultError("test draft produced no changes")
        _git(worktree, *_COMMIT_ARGS, "-m", message, extra_env=_COMMIT_ENV)
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()
    pathspec = tuple(paths)
    if not pathspec:
        raise VaultError("test draft produced no declared files")
    _git(worktree, "add", "-A", "--", *pathspec)
    changed = bool(
        _git(
            worktree,
            "diff",
            "--cached",
            "--name-only",
            "--",
            *pathspec,
        ).stdout.strip()
    )
    if not changed:
        raise VaultError("test draft produced no changes")
    _git(
        worktree,
        *_COMMIT_ARGS,
        "--only",
        "-m",
        message,
        "--",
        *pathspec,
        extra_env=_COMMIT_ENV,
    )
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def update_immutable_ref(vault: Path, ref: str, sha: str) -> None:
    existing = _git(
        vault, "rev-parse", "-q", "--verify", ref + "^{commit}", check=False
    )
    if existing.returncode == 0:
        current = existing.stdout.strip()
        if current != sha:
            raise VaultError(
                "vault ref {0} already pins {1}, not {2}".format(ref, current, sha)
            )
        return
    zeros = "0" * len(sha)
    _git(vault, "update-ref", ref, sha, zeros)


def pin_object_ref(vault: Path, ref: str, sha: str) -> None:
    existing = _git(vault, "rev-parse", "-q", "--verify", ref, check=False)
    if existing.returncode == 0:
        current = existing.stdout.strip()
        if current != sha:
            raise VaultError(
                "vault ref {0} already pins {1}, not {2}".format(ref, current, sha)
            )
        return
    _git(vault, "update-ref", ref, sha)


def hash_blob(vault: Path, data: bytes) -> str:
    result = _git(vault, "hash-object", "-w", "--stdin", input=data, text=False)
    return result.stdout.decode("ascii").strip()


def cat_blob(vault: Path, object_id: str) -> bytes:
    result = _git(vault, "cat-file", "-p", object_id, text=False)
    return result.stdout


def list_commit_blobs(vault: Path, commit: str) -> tuple[tuple[str, str], ...]:
    listed = _git(vault, "ls-tree", "-r", "--full-tree", commit)
    out = []
    for line in listed.stdout.splitlines():
        meta, _, path = line.partition("\t")
        try:
            _mode, kind, object_id = meta.split(" ", 2)
        except ValueError:
            continue
        if kind == "blob":
            out.append((path, object_id))
    return tuple(out)


def _archive_destination(root: Path, name: str) -> Path:
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if not trimmed or any(part in ("", ".", "..") for part in parts):
        raise VaultError("unsafe path in private-test archive: {0}".format(name))
    candidate = root.joinpath(*parts)
    try:
        inside = os.path.commonpath(
            (str(root.resolve()), str(candidate.resolve(strict=False)))
        ) == str(root.resolve())
    except ValueError:
        inside = False
    if not inside:
        raise VaultError("unsafe path in private-test archive: {0}".format(name))
    return candidate


def _extract_archive(tar: tarfile.TarFile, dest: Path) -> None:
    root = dest.resolve()
    for member in tar:
        target = _archive_destination(root, member.name)
        if member.isdir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise VaultError(
                    "archive directory conflicts with materialized path: {0}".format(
                        member.name
                    )
                )
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise VaultError(
                "duplicate path in private-test archive: {0}".format(member.name)
            )
        if member.isreg():
            source = tar.extractfile(member)
            if source is None:
                raise VaultError(
                    "private-test archive file has no payload: {0}".format(member.name)
                )
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)
            continue
        if member.issym():
            link_target = Path(member.linkname)
            if link_target.is_absolute():
                raise VaultError(
                    "unsafe link in private-test archive: {0}".format(member.name)
                )
            resolved_link = (target.parent / link_target).resolve(strict=False)
            try:
                inside = os.path.commonpath(
                    (str(root), str(resolved_link))
                ) == str(root)
            except ValueError:
                inside = False
            if not inside:
                raise VaultError(
                    "unsafe link in private-test archive: {0}".format(member.name)
                )
            target.symlink_to(member.linkname)
            continue
        raise VaultError(
            "unsupported entry in private-test archive: {0}".format(member.name)
        )


def _extract_commit(repo: Path, sha: str, dest: Path) -> Path:
    archive = _git(repo, "archive", "--format=tar", sha, text=False)
    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as tar:
        _extract_archive(tar, dest)
    if (dest / ".git").exists():
        raise VaultError("materialized tree carried a .git directory")
    return dest.resolve()


def materialize_commit(repo: Path, sha: str, dest: Path) -> Path:
    """Extract a sealed tree into a new or pre-provisioned empty role cwd."""
    dest = Path(dest)
    if dest.exists():
        if dest.is_symlink() or not dest.is_dir() or any(dest.iterdir()):
            raise VaultError("refusing to adopt existing tree {0}".format(dest))
    else:
        dest.mkdir(parents=True)
    return _extract_commit(repo, sha, dest)


def refresh_materialized_commit(repo: Path, sha: str, dest: Path) -> Path:
    """Replace one private tree without replacing its process-bound root inode."""
    dest = Path(dest)
    if dest.is_symlink() or not dest.is_dir():
        raise VaultError("refusing to refresh non-directory tree {0}".format(dest))
    for child in dest.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    return _extract_commit(repo, sha, dest)


def copy_blobs_to_tree(vault: Path, dest: Path, files: Mapping[str, str]) -> None:
    root = Path(dest).resolve()
    for rel_path, blob_id in files.items():
        target = (root / rel_path).resolve()
        if not str(target).startswith(str(root) + os.sep) and target != root:
            raise VaultError("refusing path {0} outside {1}".format(rel_path, root))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(cat_blob(vault, blob_id))


def linked_worktree(repo: Path, dest: Path, base_sha: str) -> Path:
    """A worktree sharing `repo`'s object database — the builder leak surface."""
    dest = Path(dest)
    if dest.exists():
        raise VaultError("refusing to adopt existing worktree {0}".format(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(dest), base_sha)
    return dest.resolve()
