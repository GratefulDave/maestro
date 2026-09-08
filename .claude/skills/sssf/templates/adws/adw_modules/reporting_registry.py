"""Observational ~/.maestro/registry.json writer. Not workflow authority."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from collections.abc import Mapping
from typing import Any

_REGISTRY_RELATIVE = Path(".maestro") / "registry.json"


def registry_path() -> Path:
    override = os.environ.get("MAESTRO_REGISTRY")
    if override:
        return Path(override)
    home = Path.home()
    return home / _REGISTRY_RELATIVE


def _canonical(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _warn(detail: str) -> None:
    path = registry_path()
    sys.stderr.write("reporting registry {0}: {1}\n".format(path, detail))



def read_run(database: Path, run_id: str) -> dict[str, Any] | None:
    """Read discovery evidence without creating or migrating a ledger."""
    if not database.is_file():
        return None
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def registered_run(run_id: str) -> tuple[Path, dict[str, Any]] | None:
    """Registry paths locate ledgers; only persisted rows identify runs.

    Missing installations are stale discovery entries. Malformed registries
    and unreadable ledgers must not silently select a different installation.
    """
    payload = _read_unlocked(registry_path())
    installations = payload.get("installations", [])
    if not isinstance(installations, list):
        raise ValueError("registry installations is not a list")
    seen: set[Path] = set()
    matches = []
    for item in installations:
        if not isinstance(item, Mapping) or not isinstance(item.get("database"), str):
            raise ValueError("registry installation has no database path")
        if not item["database"]:
            raise ValueError("registry installation has empty database path")
        database = Path(item["database"]).expanduser().resolve()
        if database in seen:
            continue
        seen.add(database)
        row = read_run(database, run_id)
        if row is not None:
            matches.append((database, row))
    if len(matches) > 1:
        raise ValueError("ambiguous registered run {0}".format(run_id))
    return matches[0] if matches else None

def register_installation(
    *,
    database: str | Path,
    plans_dir: str | Path,
    repository: str | Path,
    state: str | Path,
) -> None:
    """Record one installation. Fail-open: never refuse a run."""
    try:
        _register_installation(
            database=_canonical(database),
            plans_dir=_canonical(plans_dir),
            repository=_canonical(repository),
            state=_canonical(state),
        )
    except Exception as exc:
        _warn("{0}: {1}".format(type(exc).__name__, exc))


def _register_installation(
    *,
    database: str,
    plans_dir: str,
    repository: str,
    state: str,
) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        payload = _read_unlocked(path)
        installations = payload.get("installations")
        if not isinstance(installations, list):
            installations = []
        kept: list[Any] = []
        for item in installations:
            if not isinstance(item, Mapping):
                kept.append(item)
                continue
            existing = item.get("database")
            if isinstance(existing, str) and existing:
                try:
                    if _canonical(existing) == database:
                        continue
                except (OSError, RuntimeError, ValueError):
                    pass
            kept.append(item)
        kept.insert(
            0,
            {
                "database": database,
                "plans_dir": plans_dir,
                "repository": repository,
                "state": state,
            },
        )
        payload["installations"] = kept
        _write_unlocked(path, payload)
    finally:
        os.close(fd)


def _read_unlocked(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("registry root is not an object")
    return parsed


def _write_unlocked(path: Path, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    directory = str(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
