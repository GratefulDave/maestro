"""Descriptor-safe Maestro publication journal and worktree synchronizer.

Journal children live under ``<target_worktree_git_dir>/maestro-sync``.
Operations use directory descriptors and no-follow ``*at`` calls. Path-based
``reset`` / ``checkout`` / ``read-tree -u`` / ``clean`` are not used.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from .git_helper import BoundGit
from .receipt_crypto import NO_BLOB
from .scheduler_types import CANONICAL_SCHEMA_VERSION, canonical_bytes, digest_canonical

_JOURNAL_NAME = "maestro-sync"
_RENAME_EXCL = 0x0004
_RENAME_NOREPLACE = 1
_libc = ctypes.CDLL(None, use_errno=True)


class JournalError(RuntimeError):
    """Journal or descriptor-relative worktree mutation refused."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def open_directory_nofollow(path: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise JournalError("DIRECTORY_OPEN_REFUSED", f"{path}:{exc}") from exc


def fstat_identity(fd: int) -> os.stat_result:
    return os.fstat(fd)


def lstat_at(dir_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def require_dir_mode_0700(st: os.stat_result, *, uid: int) -> None:
    mode = stat.S_IMODE(st.st_mode)
    if not stat.S_ISDIR(st.st_mode):
        raise JournalError("JOURNAL_NOT_DIRECTORY", "")
    if st.st_uid != uid:
        raise JournalError("JOURNAL_OWNER_REFUSED", str(st.st_uid))
    if mode != 0o700:
        raise JournalError("JOURNAL_MODE_REFUSED", oct(mode))


def journal_fingerprint_payload(st: os.stat_result) -> dict[str, Any]:
    return {
        "device": st.st_dev,
        "inode": st.st_ino,
        "mode": "0700",
        "owner_uid": st.st_uid,
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "type": "directory",
    }


def journal_fingerprint(st: os.stat_result) -> str:
    return digest_canonical(journal_fingerprint_payload(st))


def mkdirat_0700(dir_fd: int, name: str) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=dir_fd)
    except FileExistsError:
        pass
    child = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
    )
    try:
        os.fchmod(child, 0o700)
        require_dir_mode_0700(os.fstat(child), uid=os.geteuid())
    finally:
        os.close(child)


def renameat_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    src_b = src.encode("utf-8")
    dst_b = dst.encode("utf-8")
    if sys.platform == "darwin":
        fn = _libc.renameatx_np
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rc = fn(src_dir_fd, src_b, dst_dir_fd, dst_b, _RENAME_EXCL)
    else:
        fn = getattr(_libc, "renameat2", None)
        if fn is None:
            raise JournalError("RENAME_NOREPLACE_UNSUPPORTED", sys.platform)
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rc = fn(src_dir_fd, src_b, dst_dir_fd, dst_b, _RENAME_NOREPLACE)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst)


def _fsync_dir_fd(fd: int) -> None:
    os.fsync(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _write_exclusive(dir_fd: int, name: str, data: bytes, mode: int = 0o600) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, mode, dir_fd=dir_fd)
    try:
        _write_all(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_file(dir_fd: int, name: str, data: bytes, mode: int = 0o600) -> None:
    tmp = name + ".tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp, flags, mode, dir_fd=dir_fd)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    _fsync_dir_fd(dir_fd)


def git_blob_oid(data: bytes, object_format: str) -> str:
    header = f"blob {len(data)}\0".encode("utf-8")
    if object_format == "sha256":
        return hashlib.sha256(header + data).hexdigest()
    return hashlib.sha1(header + data).hexdigest()


def mode_from_lstat(st: os.stat_result) -> str:
    if stat.S_ISLNK(st.st_mode):
        return "120000"
    if stat.S_ISREG(st.st_mode):
        return "100755" if st.st_mode & 0o111 else "100644"
    raise JournalError("UNSUPPORTED_WORKTREE_TYPE", oct(st.st_mode))


@dataclass(frozen=True)
class TreeEntry:
    path: str
    mode: str
    oid: str
    kind: str  # blob | commit-unsupported


def parse_ls_tree(payload: bytes) -> dict[str, TreeEntry]:
    entries: dict[str, TreeEntry] = {}
    if not payload:
        return entries
    parts = payload.split(b"\0")
    for part in parts:
        if not part:
            continue
        meta, path_b = part.split(b"\t", 1)
        mode_b, kind_b, oid_b = meta.split(b" ")
        path = path_b.decode("utf-8")
        kind = kind_b.decode("ascii")
        if kind != "blob":
            raise JournalError("UNSUPPORTED_TREE_ENTRY", f"{path}:{kind}")
        entries[path] = TreeEntry(
            path=path,
            mode=mode_b.decode("ascii"),
            oid=oid_b.decode("ascii"),
            kind=kind,
        )
    return entries


def parse_ls_files_stage(payload: bytes) -> list[tuple[str, str, str]]:
    """Return (path, mode, oid) for stage-0 blobs in a git index listing."""
    entries: list[tuple[str, str, str]] = []
    if not payload:
        return entries
    for part in payload.split(b"\0"):
        if not part:
            continue
        meta, path_b = part.split(b"\t", 1)
        mode_b, oid_b, stage_b = meta.split(b" ")
        if stage_b != b"0":
            raise JournalError("UNSUPPORTED_INDEX_STAGE", stage_b.decode("ascii"))
        mode = mode_b.decode("ascii")
        if mode not in {"100644", "100755", "120000"}:
            raise JournalError("UNSUPPORTED_TREE_ENTRY", mode)
        entries.append((path_b.decode("utf-8"), mode, oid_b.decode("ascii")))
    return entries


@dataclass(frozen=True)
class ManifestLeaf:
    path: str
    old_mode: str
    old_oid: str
    new_mode: str
    new_oid: str
    backup_name: str
    action: str  # delete | add | replace | keep


@dataclass(frozen=True)
class DirectoryIdentity:
    path: str
    device: int
    inode: int
    mode: int
    present: bool


@dataclass
class PublicationJournal:
    git_dir_fd: int
    journal_fd: int
    run_fd: int
    run_id: str
    fingerprint: str
    journal_root: str
    run_dir_name: str


def ensure_journal_root(
    worktree_git_dir: str,
    *,
    expected_fingerprint: str | None = None,
    manifest_parent_devices: tuple[int, ...] = (),
) -> tuple[int, int, str, str]:
    """Open git-dir and ``maestro-sync``. Returns (git_dir_fd, journal_fd, fingerprint, path)."""
    git_fd = open_directory_nofollow(worktree_git_dir)
    try:
        mkdirat_0700(git_fd, _JOURNAL_NAME)
        journal_fd = os.open(
            _JOURNAL_NAME,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=git_fd,
        )
    except OSError as exc:
        os.close(git_fd)
        raise JournalError("JOURNAL_OPEN_REFUSED", str(exc)) from exc
    st = os.fstat(journal_fd)
    require_dir_mode_0700(st, uid=os.geteuid())
    for device in manifest_parent_devices:
        if device != st.st_dev:
            os.close(journal_fd)
            os.close(git_fd)
            raise JournalError("PUBLICATION_CROSS_DEVICE", str(device))
    digest = journal_fingerprint(st)
    if expected_fingerprint is not None and digest != expected_fingerprint:
        os.close(journal_fd)
        os.close(git_fd)
        raise JournalError("JOURNAL_FINGERPRINT_MISMATCH", digest)
    path = str(Path(worktree_git_dir) / _JOURNAL_NAME)
    return git_fd, journal_fd, digest, path


def _run_dir_name(run_id: str, review_input_fingerprint: str) -> str:
    if "/" in run_id or "/" in review_input_fingerprint:
        raise JournalError("INVALID_JOURNAL_NAME", run_id)
    return f"{run_id}--{review_input_fingerprint}"


def open_run_journal(
    worktree_git_dir: str,
    run_id: str,
    review_input_fingerprint: str,
    *,
    expected_fingerprint: str,
    create: bool,
) -> PublicationJournal:
    git_fd, journal_fd, digest, path = ensure_journal_root(
        worktree_git_dir, expected_fingerprint=expected_fingerprint
    )
    name = _run_dir_name(run_id, review_input_fingerprint)
    if create:
        mkdirat_0700(journal_fd, name)
    try:
        run_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=journal_fd,
        )
    except FileNotFoundError as exc:
        os.close(journal_fd)
        os.close(git_fd)
        raise JournalError("JOURNAL_MISSING", name) from exc
    return PublicationJournal(
        git_dir_fd=git_fd,
        journal_fd=journal_fd,
        run_fd=run_fd,
        run_id=run_id,
        fingerprint=digest,
        journal_root=path,
        run_dir_name=name,
    )


def close_journal(journal: PublicationJournal) -> None:
    for fd in (journal.run_fd, journal.journal_fd, journal.git_dir_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def journal_is_published(journal: PublicationJournal) -> bool:
    try:
        st = lstat_at(journal.run_fd, "published")
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode)


def write_owner_and_receipt(
    journal: PublicationJournal,
    owner: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    if journal_is_published(journal):
        return
    owner_bytes = canonical_bytes(dict(owner))
    receipt_bytes = canonical_bytes(dict(receipt))
    try:
        lstat_at(journal.run_fd, "owner.json")
    except FileNotFoundError:
        _write_exclusive(journal.run_fd, "owner.json", owner_bytes)
    try:
        lstat_at(journal.run_fd, "receipt.json")
    except FileNotFoundError:
        _write_exclusive(journal.run_fd, "receipt.json", receipt_bytes)
    _fsync_dir_fd(journal.run_fd)


def read_json_at(dir_fd: int, name: str) -> dict[str, Any]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1 << 16)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(fd)
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(payload, dict):
        raise JournalError("JOURNAL_JSON_REFUSED", name)
    return payload


def publish_journal(journal: PublicationJournal) -> None:
    if journal_is_published(journal):
        return
    _write_exclusive(journal.run_fd, "published", b"1\n")
    _fsync_dir_fd(journal.run_fd)
    _fsync_dir_fd(journal.journal_fd)
    _fsync_dir_fd(journal.git_dir_fd)


def remove_unpublished_journal(journal: PublicationJournal) -> None:
    """Remove a journal that never published (zero Git operations)."""
    if journal_is_published(journal):
        raise JournalError("JOURNAL_ALREADY_PUBLISHED", journal.run_dir_name)
    _rmtree_at(journal.journal_fd, journal.run_dir_name)


def remove_zero_operation_published_journal(journal: PublicationJournal) -> None:
    """Allowed only when refs were not mutated and no worktree op ran."""
    try:
        lstat_at(journal.run_fd, "ops.log")
        raise JournalError("JOURNAL_HAS_OPERATIONS", journal.run_dir_name)
    except FileNotFoundError:
        pass
    _rmtree_at(journal.journal_fd, journal.run_dir_name)


def _rmtree_at(parent_fd: int, name: str) -> None:
    fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    try:
        for entry in os.listdir(fd):
            st = lstat_at(fd, entry)
            if stat.S_ISDIR(st.st_mode):
                _rmtree_at(fd, entry)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def append_op(journal: PublicationJournal, record: Mapping[str, Any]) -> None:
    line = canonical_bytes(dict(record)) + b"\n"
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("ops.log", flags, 0o600, dir_fd=journal.run_fd)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def backup_name(path: str, sequence: int) -> str:
    return f"{sequence:04d}-{quote(path, safe='')}"


def build_manifest(
    old_entries: Mapping[str, TreeEntry],
    new_entries: Mapping[str, TreeEntry],
) -> list[ManifestLeaf]:
    leaves: list[ManifestLeaf] = []
    paths = sorted(
        set(old_entries) | set(new_entries),
        key=lambda p: (p.count("/"), p),
        reverse=True,
    )
    sequence = 0
    for path in paths:
        old = old_entries.get(path)
        new = new_entries.get(path)
        if (
            old is not None
            and new is not None
            and old.mode == new.mode
            and old.oid == new.oid
        ):
            action = "keep"
            backup = ""
        elif old is None and new is not None:
            action = "add"
            backup = ""
        elif old is not None and new is None:
            action = "delete"
            sequence += 1
            backup = backup_name(path, sequence)
        else:
            action = "replace"
            sequence += 1
            backup = backup_name(path, sequence)
        leaves.append(
            ManifestLeaf(
                path=path,
                old_mode=old.mode if old else "000000",
                old_oid=old.oid if old else NO_BLOB,
                new_mode=new.mode if new else "000000",
                new_oid=new.oid if new else NO_BLOB,
                backup_name=backup,
                action=action,
            )
        )
    return leaves


def write_manifest(
    journal: PublicationJournal,
    leaves: list[ManifestLeaf],
    directories: list[DirectoryIdentity],
) -> None:
    payload = {
        "directories": [
            {
                "device": item.device,
                "inode": item.inode,
                "mode": item.mode,
                "path": item.path,
                "present": item.present,
            }
            for item in directories
        ],
        "leaves": [
            {
                "action": leaf.action,
                "backup_name": leaf.backup_name,
                "new_mode": leaf.new_mode,
                "new_oid": leaf.new_oid,
                "old_mode": leaf.old_mode,
                "old_oid": leaf.old_oid,
                "path": leaf.path,
            }
            for leaf in leaves
        ],
        "schema_version": CANONICAL_SCHEMA_VERSION,
    }
    data = canonical_bytes(payload)
    try:
        lstat_at(journal.run_fd, "manifest.json")
    except FileNotFoundError:
        _write_exclusive(journal.run_fd, "manifest.json", data)
        return
    existing = read_json_at(journal.run_fd, "manifest.json")
    if canonical_bytes(existing) != data:
        raise JournalError("MANIFEST_COLLISION", journal.run_dir_name)


def load_manifest(
    journal: PublicationJournal,
) -> tuple[list[ManifestLeaf], list[DirectoryIdentity]]:
    payload = read_json_at(journal.run_fd, "manifest.json")
    leaves = [
        ManifestLeaf(
            path=item["path"],
            old_mode=item["old_mode"],
            old_oid=item["old_oid"],
            new_mode=item["new_mode"],
            new_oid=item["new_oid"],
            backup_name=item["backup_name"],
            action=item["action"],
        )
        for item in payload["leaves"]
    ]
    directories = [
        DirectoryIdentity(
            path=item["path"],
            device=item["device"],
            inode=item["inode"],
            mode=item["mode"],
            present=bool(item["present"]),
        )
        for item in payload["directories"]
    ]
    return leaves, directories


def snapshot_directories(root_fd: int, paths: list[str]) -> list[DirectoryIdentity]:
    needed = {""}
    for path in paths:
        parts = path.split("/")
        acc = []
        for part in parts[:-1]:
            acc.append(part)
            needed.add("/".join(acc))
    identities: list[DirectoryIdentity] = []
    for rel in sorted(needed, key=lambda p: (p.count("/"), p)):
        try:
            fd, st = _open_existing_dir(root_fd, rel)
        except FileNotFoundError:
            identities.append(
                DirectoryIdentity(path=rel, device=0, inode=0, mode=0, present=False)
            )
            continue
        except OSError as exc:
            raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel) from exc
        try:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
            identities.append(
                DirectoryIdentity(
                    path=rel,
                    device=st.st_dev,
                    inode=st.st_ino,
                    mode=stat.S_IMODE(st.st_mode),
                    present=True,
                )
            )
        finally:
            os.close(fd)
    return identities


def _open_existing_dir(root_fd: int, rel: str) -> tuple[int, os.stat_result]:
    if rel == "":
        st = os.fstat(root_fd)
        return os.dup(root_fd), st
    fd = os.dup(root_fd)
    try:
        for part in rel.split("/"):
            nxt = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = nxt
        return fd, os.fstat(fd)
    except OSError:
        os.close(fd)
        raise


def _open_parent(
    root_fd: int, path: str, directories: Mapping[str, DirectoryIdentity]
) -> tuple[int, str]:
    parent_rel, name = path.rsplit("/", 1) if "/" in path else ("", path)
    recorded = directories.get(parent_rel)
    if recorded is None or not recorded.present:
        raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", path)
    try:
        fd, st = _open_existing_dir(root_fd, parent_rel)
    except OSError as exc:
        raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", path) from exc
    if (
        recorded.device != st.st_dev
        or recorded.inode != st.st_ino
        or not stat.S_ISDIR(st.st_mode)
    ):
        os.close(fd)
        raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", path)
    return fd, name


def _read_blob_at(
    dir_fd: int, name: str, object_format: str, mode: str
) -> tuple[str, bytes]:
    if mode == "120000":
        target = os.readlink(name, dir_fd=dir_fd)
        data = target.encode("utf-8") if isinstance(target, str) else target
        return git_blob_oid(data, object_format), data
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1 << 20)
            if not block:
                break
            chunks.append(block)
        data = b"".join(chunks)
        return git_blob_oid(data, object_format), data
    finally:
        os.close(fd)


def _materialize_blob(journal: PublicationJournal, oid: str, data: bytes) -> None:
    blobs = "blobs"
    try:
        lstat_at(journal.run_fd, blobs)
    except FileNotFoundError:
        mkdirat_0700(journal.run_fd, blobs)
    blob_dir = os.open(
        blobs,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=journal.run_fd,
    )
    try:
        try:
            lstat_at(blob_dir, oid)
        except FileNotFoundError:
            _write_exclusive(blob_dir, oid, data, 0o644)
    finally:
        os.close(blob_dir)


def store_reviewed_blobs(
    journal: PublicationJournal,
    git: BoundGit,
    leaves: list[ManifestLeaf],
) -> None:
    for leaf in leaves:
        if leaf.new_oid == NO_BLOB:
            continue
        data = git.cat_file("blob", leaf.new_oid)
        _materialize_blob(journal, leaf.new_oid, data)


def prepare_reviewed_index(
    journal: PublicationJournal,
    git: BoundGit,
    reviewed_sha: str,
    worktree_git_dir: str,
) -> None:
    index_path = str(
        Path(journal.journal_root) / journal.run_dir_name / "reviewed.index"
    )
    if not os.path.exists(index_path):
        git.read_tree_to_index(reviewed_sha, index_path, git_dir=worktree_git_dir)


def create_index_lock(worktree_git_dir: str, journal: PublicationJournal) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open("index.lock", flags, 0o644, dir_fd=journal.git_dir_fd)
    except FileExistsError as exc:
        record = _lock_record_path_ok(journal)
        if record is None:
            raise JournalError("INDEX_LOCK_UNAVAILABLE", worktree_git_dir) from exc
        st = lstat_at(journal.git_dir_fd, "index.lock")
        if st.st_dev != record["device"] or st.st_ino != record["inode"]:
            raise JournalError("INDEX_LOCK_UNAVAILABLE", worktree_git_dir) from exc
        return
    try:
        st = os.fstat(fd)
        payload = canonical_bytes(
            {
                "device": st.st_dev,
                "inode": st.st_ino,
                "schema_version": CANONICAL_SCHEMA_VERSION,
            }
        )
        try:
            lstat_at(journal.run_fd, "index.lock.record")
        except FileNotFoundError:
            _write_exclusive(journal.run_fd, "index.lock.record", payload)
    finally:
        os.close(fd)


def _lock_record_path_ok(journal: PublicationJournal) -> dict[str, Any] | None:
    try:
        return read_json_at(journal.run_fd, "index.lock.record")
    except FileNotFoundError:
        return None


def swap_index(journal: PublicationJournal) -> None:
    reviewed = "reviewed.index"
    lock_st = lstat_at(journal.git_dir_fd, "index.lock")
    src = os.open(
        reviewed, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=journal.run_fd
    )
    try:
        dst = os.open(
            "index.lock",
            os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=journal.git_dir_fd,
        )
        try:
            while True:
                block = os.read(src, 1 << 16)
                if not block:
                    break
                _write_all(dst, block)
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)
    os.rename(
        "index.lock",
        "index",
        src_dir_fd=journal.git_dir_fd,
        dst_dir_fd=journal.git_dir_fd,
    )
    _fsync_dir_fd(journal.git_dir_fd)
    append_op(
        journal,
        {
            "action": "index_swap",
            "inode": lock_st.st_ino,
            "schema_version": CANONICAL_SCHEMA_VERSION,
        },
    )


def _ensure_dir(root_fd: int, rel: str, dir_map: dict[str, DirectoryIdentity]) -> None:
    parent_rel, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
    parent_recorded = dir_map.get(parent_rel)
    if parent_recorded is None or not parent_recorded.present:
        raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
    try:
        parent_fd, st = _open_existing_dir(root_fd, parent_rel)
    except OSError as exc:
        raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel) from exc
    try:
        if parent_recorded.device != st.st_dev or parent_recorded.inode != st.st_ino:
            raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
        recorded = dir_map.get(rel)
        try:
            child_st = lstat_at(parent_fd, name)
        except FileNotFoundError:
            child_st = None
        if child_st is not None:
            if stat.S_ISLNK(child_st.st_mode) or not stat.S_ISDIR(child_st.st_mode):
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
            if recorded is not None and recorded.present:
                if (
                    recorded.device != child_st.st_dev
                    or recorded.inode != child_st.st_ino
                ):
                    raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
            elif recorded is not None and not recorded.present:
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
        else:
            if recorded is not None and recorded.present:
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        child = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        try:
            cst = os.fstat(child)
            dir_map[rel] = DirectoryIdentity(
                path=rel,
                device=cst.st_dev,
                inode=cst.st_ino,
                mode=stat.S_IMODE(cst.st_mode),
                present=True,
            )
        finally:
            os.close(child)
        _fsync_dir_fd(parent_fd)
    finally:
        os.close(parent_fd)


def _materialize_reviewed_index(
    root_fd: int,
    journal: PublicationJournal,
    git: BoundGit,
    worktree_git_dir: str,
    object_format: str,
    dir_map: dict[str, DirectoryIdentity],
) -> None:
    index_path = str(
        Path(journal.journal_root) / journal.run_dir_name / "reviewed.index"
    )
    entries = parse_ls_files_stage(
        git.ls_files_stage_z(git_dir=worktree_git_dir, index_file=index_path)
    )
    needed_dirs: set[str] = set()
    for path, _mode, _oid in entries:
        parts = path.split("/")
        acc: list[str] = []
        for part in parts[:-1]:
            acc.append(part)
            needed_dirs.add("/".join(acc))
    for rel in sorted(needed_dirs, key=lambda p: (p.count("/"), p)):
        _ensure_dir(root_fd, rel, dir_map)
    for path, mode, oid in entries:
        data = git.cat_file("blob", oid)
        parent_fd, name = _open_parent(root_fd, path, dir_map)
        try:
            try:
                st = lstat_at(parent_fd, name)
                observed_mode = mode_from_lstat(st)
                observed_oid, _ = _read_blob_at(
                    parent_fd, name, object_format, observed_mode
                )
                if observed_mode == mode and observed_oid == oid:
                    continue
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", path)
            except FileNotFoundError:
                pass
            if mode == "120000":
                os.symlink(data.decode("utf-8"), name, dir_fd=parent_fd)
            else:
                file_mode = 0o755 if mode == "100755" else 0o644
                flags = (
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                fd = os.open(name, flags, file_mode, dir_fd=parent_fd)
                try:
                    _write_all(fd, data)
                    os.fchmod(fd, file_mode)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            _fsync_dir_fd(parent_fd)
        except OSError as exc:
            raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", path) from exc
        finally:
            os.close(parent_fd)


def synchronize_publication_worktree(
    *,
    target_root: str,
    worktree_git_dir: str,
    journal: PublicationJournal,
    object_format: str,
    git: BoundGit,
) -> None:
    leaves, directories = load_manifest(journal)
    dir_map = {item.path: item for item in directories}
    root_fd = open_directory_nofollow(target_root)
    try:
        root_st = os.fstat(root_fd)
        recorded_root = dir_map.get("")
        if (
            recorded_root is None
            or recorded_root.device != root_st.st_dev
            or recorded_root.inode != root_st.st_ino
        ):
            raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", target_root)
        mkdirat_0700(journal.run_fd, "backups")
        backup_fd = os.open(
            "backups",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=journal.run_fd,
        )
        try:
            _phase_a_backup(root_fd, backup_fd, journal, leaves, dir_map, object_format)
            _phase_a_rmdir(root_fd, leaves, dir_map)
            _materialize_reviewed_index(
                root_fd, journal, git, worktree_git_dir, object_format, dir_map
            )
        finally:
            os.close(backup_fd)
        swap_index(journal)
        git.run(
            "update-index",
            "--really-refresh",
            "-q",
            git_dir=worktree_git_dir,
        )
    finally:
        os.close(root_fd)


def _phase_a_backup(
    root_fd: int,
    backup_fd: int,
    journal: PublicationJournal,
    leaves: list[ManifestLeaf],
    dir_map: Mapping[str, DirectoryIdentity],
    object_format: str,
) -> None:
    ordered = sorted(
        (leaf for leaf in leaves if leaf.action in {"delete", "replace"}),
        key=lambda leaf: (leaf.path.count("/"), leaf.path),
        reverse=True,
    )
    for leaf in ordered:
        try:
            lstat_at(backup_fd, leaf.backup_name)
            continue
        except FileNotFoundError:
            pass
        parent_fd, name = _open_parent(root_fd, leaf.path, dir_map)
        try:
            try:
                st = lstat_at(parent_fd, name)
            except FileNotFoundError:
                raise JournalError(
                    "PUBLICATION_WORKTREE_SYNC_REFUSED", leaf.path
                ) from None
            observed_mode = mode_from_lstat(st)
            oid, _data = _read_blob_at(parent_fd, name, object_format, observed_mode)
            if observed_mode != leaf.old_mode or oid != leaf.old_oid:
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", leaf.path)
            renameat_noreplace(parent_fd, name, backup_fd, leaf.backup_name)
            _fsync_dir_fd(parent_fd)
            _fsync_dir_fd(backup_fd)
            verify_st = lstat_at(backup_fd, leaf.backup_name)
            verify_mode = mode_from_lstat(verify_st)
            verify_oid, _ = _read_blob_at(
                backup_fd, leaf.backup_name, object_format, verify_mode
            )
            if verify_mode != leaf.old_mode or verify_oid != leaf.old_oid:
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", leaf.path)
            append_op(
                journal,
                {
                    "action": "backup",
                    "backup_name": leaf.backup_name,
                    "path": leaf.path,
                    "schema_version": CANONICAL_SCHEMA_VERSION,
                },
            )
        finally:
            os.close(parent_fd)


def _phase_a_rmdir(
    root_fd: int,
    leaves: list[ManifestLeaf],
    dir_map: Mapping[str, DirectoryIdentity],
) -> None:
    old_dirs = {path for path, ident in dir_map.items() if path and ident.present}
    new_dirs: set[str] = set()
    for leaf in leaves:
        if leaf.action in {"add", "replace", "keep"}:
            parts = leaf.path.split("/")
            acc = []
            for part in parts[:-1]:
                acc.append(part)
                new_dirs.add("/".join(acc))
    removable = sorted(
        old_dirs - new_dirs, key=lambda p: (p.count("/"), p), reverse=True
    )
    for rel in removable:
        parent_rel, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
        try:
            parent_fd, st = _open_existing_dir(root_fd, parent_rel)
        except FileNotFoundError:
            continue
        try:
            recorded = dir_map.get(parent_rel)
            if (
                recorded is None
                or recorded.device != st.st_dev
                or recorded.inode != st.st_ino
            ):
                continue
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno not in {errno.ENOTEMPTY, errno.ENOENT}:
                    raise JournalError(
                        "PUBLICATION_WORKTREE_SYNC_REFUSED", rel
                    ) from exc
        finally:
            os.close(parent_fd)


def _phase_b_install(
    root_fd: int,
    journal: PublicationJournal,
    leaves: list[ManifestLeaf],
    dir_map: dict[str, DirectoryIdentity],
    object_format: str,
) -> None:
    needed_dirs: set[str] = set()
    for leaf in leaves:
        if leaf.action in {"add", "replace"}:
            parts = leaf.path.split("/")
            acc: list[str] = []
            for part in parts[:-1]:
                acc.append(part)
                needed_dirs.add("/".join(acc))
    for rel in sorted(needed_dirs, key=lambda p: (p.count("/"), p)):
        parent_rel, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
        parent_recorded = dir_map.get(parent_rel)
        if parent_recorded is None or not parent_recorded.present:
            raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
        try:
            parent_fd, st = _open_existing_dir(root_fd, parent_rel)
        except OSError as exc:
            raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel) from exc
        try:
            if (
                parent_recorded.device != st.st_dev
                or parent_recorded.inode != st.st_ino
            ):
                raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
            recorded = dir_map.get(rel)
            try:
                child_st = lstat_at(parent_fd, name)
            except FileNotFoundError:
                child_st = None
            if child_st is not None:
                if stat.S_ISLNK(child_st.st_mode) or not stat.S_ISDIR(child_st.st_mode):
                    raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
                if recorded is None or not recorded.present:
                    raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
                if (
                    recorded.device != child_st.st_dev
                    or recorded.inode != child_st.st_ino
                ):
                    raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
            else:
                if recorded is not None and recorded.present:
                    raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", rel)
                os.mkdir(name, 0o755, dir_fd=parent_fd)
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            try:
                cst = os.fstat(child)
                dir_map[rel] = DirectoryIdentity(
                    path=rel,
                    device=cst.st_dev,
                    inode=cst.st_ino,
                    mode=stat.S_IMODE(cst.st_mode),
                    present=True,
                )
            finally:
                os.close(child)
            _fsync_dir_fd(parent_fd)
        finally:
            os.close(parent_fd)

    blob_dir = os.open(
        "blobs",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=journal.run_fd,
    )
    try:
        ordered = sorted(
            (leaf for leaf in leaves if leaf.action in {"add", "replace"}),
            key=lambda leaf: (leaf.path.count("/"), leaf.path),
        )
        for leaf in ordered:
            parent_fd, name = _open_parent(root_fd, leaf.path, dir_map)
            try:
                try:
                    st = lstat_at(parent_fd, name)
                    observed_mode = mode_from_lstat(st)
                    oid, _ = _read_blob_at(
                        parent_fd, name, object_format, observed_mode
                    )
                    if observed_mode == leaf.new_mode and oid == leaf.new_oid:
                        continue
                    raise JournalError("PUBLICATION_WORKTREE_SYNC_REFUSED", leaf.path)
                except FileNotFoundError:
                    pass
                data = _read_blob_file(blob_dir, leaf.new_oid)
                if leaf.new_mode == "120000":
                    os.symlink(data.decode("utf-8"), name, dir_fd=parent_fd)
                else:
                    mode = 0o755 if leaf.new_mode == "100755" else 0o644
                    flags = (
                        os.O_CREAT
                        | os.O_EXCL
                        | os.O_WRONLY
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC
                    )
                    fd = os.open(name, flags, mode, dir_fd=parent_fd)
                    try:
                        os.write(fd, data)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                _fsync_dir_fd(parent_fd)
                append_op(
                    journal,
                    {
                        "action": "install",
                        "path": leaf.path,
                        "schema_version": CANONICAL_SCHEMA_VERSION,
                    },
                )
            except OSError as exc:
                raise JournalError(
                    "PUBLICATION_WORKTREE_SYNC_REFUSED", leaf.path
                ) from exc
            finally:
                os.close(parent_fd)
    finally:
        os.close(blob_dir)


def _read_blob_file(blob_dir: int, oid: str) -> bytes:
    fd = os.open(oid, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=blob_dir)
    try:
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1 << 20)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def cleanup_success(journal: PublicationJournal) -> None:
    """Remove manifest-owned backups/staging/lock after ledger completion."""
    for name in ("backups", "blobs"):
        try:
            lstat_at(journal.run_fd, name)
        except FileNotFoundError:
            continue
        _rmtree_at(journal.run_fd, name)
    for name in ("reviewed.index", "ops.log"):
        try:
            os.unlink(name, dir_fd=journal.run_fd)
        except FileNotFoundError:
            pass
    try:
        os.unlink("index.lock", dir_fd=journal.git_dir_fd)
    except FileNotFoundError:
        pass


def decode_backup_name(name: str) -> str:
    _, encoded = name.split("-", 1)
    return unquote(encoded)
