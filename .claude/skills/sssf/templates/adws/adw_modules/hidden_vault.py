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
) -> subprocess.CompletedProcess:
    if text is None:
        text = not isinstance(input, (bytes, bytearray))
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
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


def ensure_vault(state_root: Path, run_id: str) -> Path:
    """The run's bare vault, created if absent. Idempotent. Mode 0700."""
    path = vault_path(state_root, run_id)
    parent = path.parent
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
    _git(
        vault,
        "fetch",
        "--no-tags",
        str(Path(repo).resolve()),
        "+{0}:{1}".format(integration_ref, dest),
    )
    return _git(vault, "rev-parse", dest).stdout.strip()


def draft_ref(run_id: str, lane_id: str, input_digest: str) -> str:
    return "refs/maestro/drafts/{0}/{1}/{2}".format(
        _require_id("run_id", run_id),
        _require_id("lane_id", lane_id),
        _require_digest(input_digest),
    )


def sealed_ref(run_id: str, lane_id: str, input_digest: str) -> str:
    return "refs/maestro/sealed/{0}/{1}/{2}".format(
        _require_id("run_id", run_id),
        _require_id("lane_id", lane_id),
        _require_digest(input_digest),
    )


def private_results_ref(run_id: str, lane_id: str, input_digest: str) -> str:
    return "refs/maestro/private-results/{0}/{1}/{2}".format(
        _require_id("run_id", run_id),
        _require_id("lane_id", lane_id),
        _require_digest(input_digest),
    )


def _require_digest(value: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise VaultError("input_digest {0!r} is not SHA-256 hex".format(value))
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


def commit_all(worktree: Path, message: str) -> str:
    _git(worktree, "config", "user.email", "maestro-private-review@invalid")
    _git(worktree, "config", "user.name", "maestro-private-review")
    _git(worktree, "add", "-A")
    status = _git(worktree, "status", "--porcelain")
    if not status.stdout.strip():
        raise VaultError("test draft produced no changes")
    _git(worktree, "commit", "-qm", message)
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
