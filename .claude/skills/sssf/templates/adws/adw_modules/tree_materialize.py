"""Materialize one commit of the run repository into a tree the factory owns.

Every harness tree -- a review tree, a draft-collect tree, a runner-preflight
tree -- is an extract of one immutable commit into a directory under the
deployment's `runtime_state_root`. The extract is made a one-commit git
repository of exactly its files, so repository tests that ask git about the
checkout (`git grep`, `git ls-files`) get an answer, and nothing else: no
remote, no history, no ref into the run repository.

This module used to be the materialization half of `hidden_vault`, which also
owned a bare object database the accepted test suite was kept in so a builder
could not read it. Accepted tests are visible now -- they are carried in the
builder's checkout and pinned by digest (`test_binding`) -- so the vault is
gone and only the materialization survives, under a name that says what it is.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Mapping, Sequence

from .runtime_state import paths_overlap


class MaterializeError(RuntimeError):
    """A tree could not be extracted, cleared, or made a repository."""


class TreeContainmentRefused(MaterializeError):
    """A materialization destination lies outside what the factory owns."""

    code = "MATERIALIZED_TREE_UNCONTAINED"


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
        raise MaterializeError(
            "git {0} in {1} exited {2}: {3}".format(
                " ".join(args), cwd, result.returncode, (err or "").strip()
            )
        )
    return result


def rev_parse(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", rev).stdout.strip()


def cat_blob(repo: Path, object_id: str) -> bytes:
    """The bytes of one blob in `repo`'s object database."""
    return _git(repo, "cat-file", "-p", object_id, text=False).stdout


def scratch_tree_path(worktrees_root: Path, prefix: str) -> Path:
    """A path for a tree that exists only while one call runs.

    The name carries no identity: two calls over the same input get two names,
    so a tree left behind by a crashed call can never be the one a retry is
    refused for adopting. The directory is scaffolding and is removed by
    `remove_tree` when the call ends -- unless the call is keeping it as the
    evidence of a harness failure, which the randomized name is also what
    makes safe.
    """
    return Path(worktrees_root) / "{0}-{1}".format(prefix, os.urandom(8).hex())


def remove_tree(dest: Path) -> None:
    """Drop a scratch tree; never raises."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)


def _archive_destination(root: Path, name: str) -> Path:
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if not trimmed or any(part in ("", ".", "..") for part in parts):
        raise MaterializeError("unsafe path in tree archive: {0}".format(name))
    candidate = root.joinpath(*parts)
    try:
        inside = os.path.commonpath(
            (str(root.resolve()), str(candidate.resolve(strict=False)))
        ) == str(root.resolve())
    except ValueError:
        inside = False
    if not inside:
        raise MaterializeError("unsafe path in tree archive: {0}".format(name))
    return candidate


def _extract_archive(tar: tarfile.TarFile, dest: Path) -> None:
    root = dest.resolve()
    for member in tar:
        target = _archive_destination(root, member.name)
        if member.isdir():
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise MaterializeError(
                    "archive directory conflicts with materialized path: {0}".format(
                        member.name
                    )
                )
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise MaterializeError(
                "duplicate path in tree archive: {0}".format(member.name)
            )
        if member.isreg():
            source = tar.extractfile(member)
            if source is None:
                raise MaterializeError(
                    "tree archive file has no payload: {0}".format(member.name)
                )
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)
            continue
        if member.issym():
            link_target = Path(member.linkname)
            if link_target.is_absolute():
                raise MaterializeError(
                    "unsafe link in tree archive: {0}".format(member.name)
                )
            resolved_link = (target.parent / link_target).resolve(strict=False)
            try:
                inside = os.path.commonpath(
                    (str(root), str(resolved_link))
                ) == str(root)
            except ValueError:
                inside = False
            if not inside:
                raise MaterializeError(
                    "unsafe link in tree archive: {0}".format(member.name)
                )
            target.symlink_to(member.linkname)
            continue
        raise MaterializeError(
            "unsupported entry in tree archive: {0}".format(member.name)
        )


def _source_repository_paths(repo: Path) -> tuple[str, ...]:
    """The source repository's work tree, git dir and common dir, absolute."""
    found = _git(
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        "--git-common-dir",
    ).stdout.split("\n")
    top = _git(repo, "rev-parse", "--show-toplevel", check=False)
    if top.returncode == 0:
        found.append(top.stdout)
    return tuple(item.strip() for item in found if item.strip())


def _refuse_uncontained_tree(
    repo: Path, dest: Path, state_root: Path, forbidden: Sequence[Path]
) -> None:
    """Refuse, before touching `dest`, a tree the factory does not own.

    A materialized tree is emptied, extracted into, and made a git repository.
    Aimed anywhere but a private directory under the runtime-state root, each
    of those is a write to someone else's files. The overlap list is the one
    `RuntimeStateRoot` refuses, plus the source repository's own git dirs and
    the checkout this runtime copy is running from.
    """
    tree = Path(os.path.realpath(dest))
    root = Path(os.path.realpath(state_root))
    if tree == root or not tree.is_relative_to(root):
        raise TreeContainmentRefused(
            "materialized tree {0} is not inside runtime_state_root {1}".format(
                tree, root
            )
        )
    runtime_copy = Path(__file__).resolve().parents[1]
    runtime_top = _git(runtime_copy, "rev-parse", "--show-toplevel", check=False)
    others = [*forbidden, *_source_repository_paths(repo), runtime_copy]
    if runtime_top.returncode == 0 and runtime_top.stdout.strip():
        others.append(Path(runtime_top.stdout.strip()))
    for other in others:
        if paths_overlap(tree, os.path.realpath(other)):
            raise TreeContainmentRefused(
                "materialized tree {0} overlaps {1}".format(tree, other)
            )


def _tree_git_environment(dest: Path) -> dict[str, str]:
    """An environment in which git can see only `dest` and its own `.git`.

    Every inherited `GIT_*` variable is dropped: a caller's `GIT_DIR` or
    `GIT_INDEX_FILE` would otherwise point `init` and `add` at another
    repository. Global and system config are off, so a user's `core.hooksPath`,
    `init.templateDir`, `core.excludesFile`, `commit.gpgSign` or LFS filters
    cannot add hooks, drop a materialized file from the index, or block the
    commit. The ceiling stops discovery at the tree's parent.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_DIR": str(dest / ".git"),
            "GIT_WORK_TREE": str(dest),
            "GIT_CEILING_DIRECTORIES": str(dest.parent),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "maestro",
            "GIT_AUTHOR_EMAIL": "maestro@materialized.invalid",
            "GIT_COMMITTER_NAME": "maestro",
            "GIT_COMMITTER_EMAIL": "maestro@materialized.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )
    return env


def _commit_materialized_tree(dest: Path) -> None:
    """Make `dest` a repository holding one commit of exactly its files.

    Repository tests run against a real checkout, and some ask git about it:
    FDAdb's `deploy/tests/runtime-wiring.test.mjs` runs `git grep`, which
    fails with "not a git repository" in a bare archive export. `git archive`
    exports tracked files only, so one commit of everything extracted is the
    set `git grep` would search in the source checkout. No remote, no history:
    the only object reachable here is that one commit.
    """
    env = _tree_git_environment(dest)
    for argv in (
        ("init", "-q", "--template=", "--initial-branch=materialized"),
        ("add", "-A", "--force", "--", "."),
        ("commit", "-q", "--no-verify", "--no-gpg-sign", "--allow-empty", "-m", "materialized"),
    ):
        result = subprocess.run(
            ["git", *argv], cwd=str(dest), capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            raise MaterializeError(
                "git {0} in materialized tree {1} exited {2}: {3}".format(
                    " ".join(argv), dest, result.returncode, result.stderr.strip()
                )
            )


def _extract_commit(repo: Path, sha: str, dest: Path) -> Path:
    archive = _git(repo, "archive", "--format=tar", sha, text=False)
    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as tar:
        _extract_archive(tar, dest)
    if (dest / ".git").exists():
        raise MaterializeError("materialized tree carried a .git directory")
    _commit_materialized_tree(dest.resolve())
    return dest.resolve()


def materialize_commit(
    repo: Path,
    sha: str,
    dest: Path,
    *,
    state_root: Path,
    forbidden: Sequence[Path] = (),
) -> Path:
    """Extract one commit into a new or pre-provisioned empty tree."""
    dest = Path(dest)
    _refuse_uncontained_tree(repo, dest, state_root, forbidden)
    if dest.exists():
        if dest.is_symlink() or not dest.is_dir() or any(dest.iterdir()):
            raise MaterializeError("refusing to adopt existing tree {0}".format(dest))
    else:
        dest.mkdir(parents=True)
    return _extract_commit(repo, sha, dest)


def clear_tree(dest: Path) -> None:
    """Empty `dest`, keeping its root inode, even past read-only directories."""

    def _owner_writable_parent(func, path, exc_info):
        if not isinstance(exc_info[1], PermissionError):
            raise exc_info[1]
        parent = os.path.dirname(path)
        mode = os.lstat(parent).st_mode
        if stat.S_ISLNK(mode):
            raise exc_info[1]
        os.chmod(parent, stat.S_IMODE(mode) | stat.S_IWUSR)
        func(path)

    for child in Path(dest).iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child, onerror=_owner_writable_parent)


def refresh_materialized_commit(
    repo: Path,
    sha: str,
    dest: Path,
    *,
    state_root: Path,
    forbidden: Sequence[Path] = (),
) -> Path:
    """Replace one materialized tree without replacing its process-bound root inode."""
    dest = Path(dest)
    _refuse_uncontained_tree(repo, dest, state_root, forbidden)
    if dest.is_symlink() or not dest.is_dir():
        raise MaterializeError("refusing to refresh non-directory tree {0}".format(dest))
    clear_tree(dest)
    return _extract_commit(repo, sha, dest)
