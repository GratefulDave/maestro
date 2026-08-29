"""Descriptor-bound external runtime-state root. Not workflow authority."""

from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from .scheduler_types import KernelError, runtime_state_fingerprint

LEDGER_FILENAME = "lifecycle.sqlite3"
RUNTIME_STATE_MODE = 0o700
LAYOUT_CHILDREN = (
    "artifacts",
    "vault",
    "locks",
    "receipts",
    "plans",
    "worktrees",
)


class RuntimeStateRefused(KernelError):
    code = "RUNTIME_STATE_REFUSED"


def _abspath(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def paths_overlap(left: str | Path, right: str | Path) -> bool:
    a = _abspath(left)
    b = _abspath(right)
    if a == b:
        return True
    try:
        a.relative_to(b)
        return True
    except ValueError:
        try:
            b.relative_to(a)
            return True
        except ValueError:
            return False


def _rewrite_root_aliases(abs_path: Path) -> Path:
    """Replace leading OS location aliases such as /var -> /private/var.

    Only children of the filesystem root are rewritten. Symlinks beneath that
    canonical ancestor remain in the walk and are refused by O_NOFOLLOW.
    """
    root = Path(abs_path.anchor)
    rest = list(abs_path.parts[1:])
    current = root
    while rest:
        candidate = current / rest[0]
        if candidate.is_symlink() and candidate.parent == root:
            current = Path(os.path.realpath(candidate))
            rest = rest[1:]
            continue
        break
    return current.joinpath(*rest) if rest else current


def _open_directory_nofollow(path: Path) -> int:
    if not path.is_absolute():
        raise RuntimeStateRefused("path is not absolute")
    if path.is_symlink():
        raise RuntimeStateRefused("symlink component")
    walk = _rewrite_root_aliases(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    nofollow = flags | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(Path(walk.anchor)), flags)
    try:
        for name in walk.parts[1:]:
            try:
                nxt = os.open(name, nofollow, dir_fd=fd)
            except OSError as exc:
                os.close(fd)
                fd = -1
                if exc.errno in (errno.ELOOP, errno.EPERM):
                    raise RuntimeStateRefused("symlink component") from exc
                raise RuntimeStateRefused("cannot open directory") from exc
            os.close(fd)
            fd = nxt
        return fd
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise


def _require_owned_mode(st_result: os.stat_result) -> None:
    if not stat.S_ISDIR(st_result.st_mode):
        raise RuntimeStateRefused("not a directory")
    if stat.S_IMODE(st_result.st_mode) != RUNTIME_STATE_MODE:
        raise RuntimeStateRefused("mode is not 0700")
    if st_result.st_uid != os.geteuid():
        raise RuntimeStateRefused("not owned by effective user")


class RuntimeStateRoot:
    def __init__(
        self,
        path: str | Path,
        *,
        overlap_paths: Sequence[str | Path] = (),
    ) -> None:
        self.path = _abspath(path)
        if not self.path.is_absolute():
            raise RuntimeStateRefused("path is not absolute")
        for other in overlap_paths:
            if paths_overlap(self.path, other):
                raise RuntimeStateRefused("overlaps a forbidden path")
        self._fd = _open_directory_nofollow(self.path)
        try:
            info = os.fstat(self._fd)
            _require_owned_mode(info)
            self.device = info.st_dev
            self.inode = info.st_ino
            self.fingerprint = runtime_state_fingerprint(
                str(self.path), self.device, self.inode
            )
        except Exception:
            os.close(self._fd)
            raise

    def close(self) -> None:
        fd = getattr(self, "_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._fd = -1

    def __enter__(self) -> "RuntimeStateRoot":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def revalidate(self, expected_fingerprint: str) -> None:
        info = os.fstat(self._fd)
        _require_owned_mode(info)
        current = runtime_state_fingerprint(str(self.path), info.st_dev, info.st_ino)
        if (
            current != expected_fingerprint
            or info.st_dev != self.device
            or info.st_ino != self.inode
        ):
            raise RuntimeStateRefused("fingerprint mismatch")

    def ledger_path(self) -> Path:
        return self.path / LEDGER_FILENAME

    def ensure_layout(self) -> None:
        for name in LAYOUT_CHILDREN:
            try:
                os.mkdir(name, RUNTIME_STATE_MODE, dir_fd=self._fd)
            except FileExistsError:
                pass
            child = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._fd,
            )
            try:
                _require_owned_mode(os.fstat(child))
            finally:
                os.close(child)


@contextmanager
def open_runtime_state_root(
    path: str | Path,
    *,
    overlap_paths: Sequence[str | Path] = (),
) -> Iterator[RuntimeStateRoot]:
    root = RuntimeStateRoot(path, overlap_paths=overlap_paths)
    try:
        yield root
    finally:
        root.close()
