"""Bound-repository Git operations for the artifact-factory slice.

Every command uses ``git -C <repository_root>``. Process cwd is never the
repository binding. Mutable branch checkout helpers are not provided.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_HEX = re.compile(r"^[0-9a-f]+$")
_OID_LENGTHS = {40, 64}


class GitError(RuntimeError):
    """A Git command refused or the object graph did not match the contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__("{0}:{1}".format(code, detail) if detail else code)


def zero_oid(object_format: str) -> str:
    if object_format == "sha256":
        return "0" * 64
    if object_format == "sha1":
        return "0" * 40
    raise GitError("UNSUPPORTED_OBJECT_FORMAT", object_format)


def require_oid(value: str, *, object_format: str | None = None) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise GitError("INVALID_OID", repr(value))
    if object_format == "sha1" and len(value) != 40:
        raise GitError("INVALID_OID", value)
    if object_format == "sha256" and len(value) != 64:
        raise GitError("INVALID_OID", value)
    if object_format is None and len(value) not in _OID_LENGTHS:
        raise GitError("INVALID_OID", value)
    return value


def _clean_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    ):
        env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    if extra:
        env.update(extra)
    return env


@dataclass(frozen=True)
class BoundGit:
    """Git operations pinned to one canonical worktree root."""

    repository_root: Path

    def __post_init__(self) -> None:
        root = Path(self.repository_root)
        if not root.is_absolute():
            raise GitError("REPOSITORY_ROOT_NOT_ABSOLUTE", str(root))
        object.__setattr__(self, "repository_root", root)

    def run(
        self,
        *args: str,
        check: bool = True,
        input_bytes: bytes | None = None,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        git_dir: str | None = None,
        index_file: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [
            "git",
            "-C",
            str(self.repository_root),
            "-c",
            "i18n.commitEncoding=UTF-8",
        ]
        if git_dir is not None:
            argv.extend(["--git-dir", git_dir])
        argv.extend(args)
        run_env = _clean_env(extra_env)
        if env is not None:
            run_env = dict(env)
            if extra_env:
                run_env.update(extra_env)
        if index_file is not None:
            run_env["GIT_INDEX_FILE"] = index_file
        result = subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            shell=False,
            env=run_env,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            stdout = result.stdout.decode("utf-8", "replace").strip()
            raise GitError(
                "GIT_COMMAND_REFUSED",
                "{0} ({1})".format(
                    " ".join(args[:4]), stderr or stdout or str(result.returncode)
                ),
            )
        return result

    def text(self, *args: str, **kwargs: object) -> str:
        result = self.run(*args, **kwargs)  # type: ignore[arg-type]
        return result.stdout.decode("utf-8").strip()

    def object_format(self) -> str:
        value = self.text("rev-parse", "--show-object-format")
        if value not in {"sha1", "sha256"}:
            raise GitError("UNSUPPORTED_OBJECT_FORMAT", value)
        return value

    def is_bare(self) -> bool:
        return self.text("rev-parse", "--is-bare-repository") == "true"

    def git_common_dir(self) -> Path:
        return Path(
            self.text("rev-parse", "--path-format=absolute", "--git-common-dir")
        )

    def git_dir(self) -> Path:
        return Path(self.text("rev-parse", "--path-format=absolute", "--git-dir"))

    def symbolic_head(self) -> str:
        result = self.run("symbolic-ref", "-q", "HEAD", check=False)
        if result.returncode != 0:
            raise GitError("TARGET_DETACHED", "HEAD is not a symbolic ref")
        return result.stdout.decode("utf-8").strip()

    def rev_parse(self, rev: str) -> str:
        if rev.startswith("-"):
            raise GitError("INVALID_REV", rev)
        return require_oid(
            self.text("rev-parse", "--verify", "{0}^{{commit}}".format(rev))
        )

    def resolve_tree(self, rev: str) -> str:
        if rev.startswith("-"):
            raise GitError("INVALID_REV", rev)
        return self.text("rev-parse", "--verify", "{0}^{{tree}}".format(rev))

    def update_ref(self, ref: str, new: str, old: str) -> None:
        _require_ref(ref)
        require_oid(new)
        require_oid(old)
        self.run("update-ref", ref, new, old)

    def update_ref_stdin(self, script: str) -> None:
        self.run("update-ref", "--stdin", input_bytes=script.encode("utf-8"))

    def ref_exists(self, ref: str) -> bool:
        _require_ref(ref)
        result = self.run("show-ref", "--verify", "--quiet", ref, check=False)
        return result.returncode == 0

    def read_ref(self, ref: str) -> str | None:
        _require_ref(ref)
        result = self.run("rev-parse", "--verify", "--quiet", ref, check=False)
        if result.returncode != 0:
            return None
        return require_oid(result.stdout.decode("utf-8").strip())

    def merge_tree_write_tree(self, before_sha: str, candidate_sha: str) -> str:
        require_oid(before_sha)
        require_oid(candidate_sha)
        result = self.run(
            "merge-tree", "--write-tree", before_sha, candidate_sha, check=False
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
            raise GitError("MERGE_TREE_REFUSED", detail)
        tree = result.stdout.decode("utf-8").splitlines()[0].strip()
        if not _HEX.fullmatch(tree):
            raise GitError("MERGE_TREE_REFUSED", tree)
        return tree

    def commit_tree(
        self,
        tree: str,
        parents: Sequence[str],
        message: bytes,
        *,
        epoch_seconds: int,
    ) -> str:
        if not message.endswith(b"\n"):
            raise GitError(
                "MERGE_MESSAGE_ENCODING", "merge message must end with one LF"
            )
        extra = {
            "GIT_AUTHOR_NAME": "Maestro Runtime",
            "GIT_AUTHOR_EMAIL": "maestro@localhost.invalid",
            "GIT_AUTHOR_DATE": "@{0} +0000".format(epoch_seconds),
            "GIT_COMMITTER_NAME": "Maestro Runtime",
            "GIT_COMMITTER_EMAIL": "maestro@localhost.invalid",
            "GIT_COMMITTER_DATE": "@{0} +0000".format(epoch_seconds),
        }
        args: list[str] = ["commit-tree", tree]
        for parent in parents:
            require_oid(parent)
            args.extend(["-p", parent])
        args.extend(["-F", "-"])
        result = self.run(*args, input_bytes=message, extra_env=extra)
        return require_oid(result.stdout.decode("utf-8").strip())

    def hash_object(
        self, data: bytes, *, object_type: str = "blob", write: bool = True
    ) -> str:
        args = ["hash-object", "-t", object_type]
        if write:
            args.append("-w")
        args.append("--stdin")
        return require_oid(self.text(*args, input_bytes=data))

    def cat_file(self, object_type: str, oid: str) -> bytes:
        require_oid(oid)
        result = self.run("cat-file", object_type, oid)
        return result.stdout

    def commit_parents(self, sha: str) -> tuple[str, ...]:
        require_oid(sha)
        line = self.text("rev-list", "--parents", "-n", "1", sha)
        parts = line.split()
        if not parts or parts[0] != sha:
            raise GitError("COMMIT_PARENTS_REFUSED", line)
        return tuple(parts[1:])

    def commit_tree_oid(self, sha: str) -> str:
        require_oid(sha)
        return self.text("rev-parse", "{0}^{{tree}}".format(sha))

    def commit_message(self, sha: str) -> bytes:
        require_oid(sha)
        result = self.run("cat-file", "-p", sha)
        payload = result.stdout
        sep = payload.find(b"\n\n")
        if sep < 0:
            raise GitError("COMMIT_MESSAGE_REFUSED", sha)
        return payload[sep + 2 :]

    def is_ancestor(self, maybe_ancestor: str, commit: str) -> bool:
        require_oid(maybe_ancestor)
        require_oid(commit)
        result = self.run(
            "merge-base", "--is-ancestor", maybe_ancestor, commit, check=False
        )
        return result.returncode == 0

    def diff_tree_raw(self, base_sha: str, candidate_sha: str) -> bytes:
        require_oid(base_sha)
        require_oid(candidate_sha)
        result = self.run(
            "diff-tree",
            "--no-commit-id",
            "-r",
            "-M",
            "-C",
            "--find-copies-harder",
            "--raw",
            "-z",
            "--no-abbrev",
            base_sha,
            candidate_sha,
        )
        return result.stdout

    def ls_tree_z(self, treeish: str) -> bytes:
        if treeish.startswith("-"):
            raise GitError("INVALID_REV", treeish)
        result = self.run("ls-tree", "-r", "-z", treeish)
        return result.stdout

    def read_tree_to_index(
        self, treeish: str, index_file: str, *, git_dir: str
    ) -> None:
        if treeish.startswith("-"):
            raise GitError("INVALID_REV", treeish)
        self.run(
            "read-tree",
            "--reset",
            treeish,
            git_dir=git_dir,
            index_file=index_file,
        )

    def ls_files_stage_z(self, *, git_dir: str, index_file: str) -> bytes:
        result = self.run(
            "ls-files",
            "--stage",
            "-z",
            git_dir=git_dir,
            index_file=index_file,
        )
        return result.stdout

    def diff_cached_quiet(
        self, expected_before: str, *, git_dir: str | None = None
    ) -> bool:
        require_oid(expected_before)
        result = self.run(
            "diff",
            "--cached",
            "--quiet",
            expected_before,
            "--",
            check=False,
            git_dir=git_dir,
        )
        return result.returncode == 0

    def diff_files_quiet(self, *, git_dir: str | None = None) -> bool:
        result = self.run("diff-files", "--quiet", check=False, git_dir=git_dir)
        return result.returncode == 0

    def ls_others(
        self, *, ignored: bool, git_dir: str | None = None
    ) -> tuple[str, ...]:
        args = ["ls-files", "--others", "--exclude-standard"]
        if ignored:
            args.append("--ignored")
        result = self.run(*args, git_dir=git_dir)
        paths = [line.decode("utf-8") for line in result.stdout.split(b"\n") if line]
        return tuple(paths)


def _require_ref(ref: str) -> None:
    if (
        not isinstance(ref, str)
        or not ref.startswith("refs/")
        or ".." in ref
        or "\\" in ref
    ):
        raise GitError("INVALID_REF", ref)
    if any(part in {".", ""} for part in ref.split("/")):
        raise GitError("INVALID_REF", ref)
