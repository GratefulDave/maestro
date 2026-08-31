"""Detach the Maestro dashboard launcher from run start/resume. Fail-open."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from adw_modules.reporting_registry import registry_path

Popen = Callable[..., Any]


def dashboard_warning(detail: str) -> None:
    sys.stderr.write("dashboard: {0}\n".format(detail))


def launcher_is_usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def build_launcher_argv(cfg: Mapping[str, Any], ledger: Path) -> list[str]:
    argv = [
        str(cfg["launcher"]),
        "--ledger",
        str(Path(ledger).expanduser().resolve()),
        "--api-port",
        str(cfg["api_port"]),
        "--ui-port",
        str(cfg["ui_port"]),
    ]
    if cfg.get("open", True):
        argv.append("--open")
    else:
        argv.append("--no-open")
    return argv


def build_launcher_env(cfg: Mapping[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["MAESTRO_REGISTRY"] = str(registry_path())
    env["MAESTRO_API_PORT"] = str(cfg["api_port"])
    env["MAESTRO_UI_PORT"] = str(cfg["ui_port"])
    return env


def spawn_dashboard_launcher(
    cfg: Mapping[str, Any],
    *,
    repository: Path,
    ledger: Path,
    popen: Popen = subprocess.Popen,
) -> None:
    argv = build_launcher_argv(cfg, ledger)
    env = build_launcher_env(cfg)
    popen(
        argv,
        cwd=str(Path(repository).resolve()),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def maybe_autoload_dashboard(
    layout: Mapping[str, Any] | None,
    *,
    repository: Path,
    ledger: Path,
    popen: Popen = subprocess.Popen,
) -> None:
    """Fire-and-forget dashboard start. Never blocks the factory scheduler."""
    cfg = None if layout is None else layout.get("dashboard")
    if not cfg or not cfg.get("enabled"):
        return
    raw_launcher = cfg.get("launcher")
    if not isinstance(raw_launcher, str) or not raw_launcher.strip():
        dashboard_warning("enabled but launcher is missing; continuing")
        return
    launcher = Path(raw_launcher)
    if not launcher.is_absolute() or not launcher_is_usable(launcher):
        dashboard_warning(
            "invalid launcher {0}; continuing".format(raw_launcher)
        )
        return
    try:
        spawn_dashboard_launcher(
            cfg, repository=repository, ledger=ledger, popen=popen
        )
    except OSError as exc:
        dashboard_warning("failed to spawn launcher: {0}; continuing".format(exc))
