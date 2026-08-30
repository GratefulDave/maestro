"""Hard container boundary for Maestro role agents.

The authenticated harness remains on the host. Every model-controlled shell command
executes in a no-network, read-only-root container with only its role checkout mounted.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


class IsolationRefused(RuntimeError):
    """The requested operation crosses the assigned checkout boundary."""


_AGENT_DIR = ".maestro-agent"
_SANDBOX_IMAGE = "maestro-role-sandbox:2026-08-30"
_SAFE_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "FORCE_COLOR",
    "PYTEST_ADDOPTS",
    "PYTHONPYCACHEPREFIX",
    "RUFF_CACHE_DIR",
    "npm_config_cache",
)
_SECRET_ENV = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|CREDENTIAL|AUTH)", re.IGNORECASE
)
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def agent_dir(root: str | Path) -> Path:
    return Path(root).resolve() / _AGENT_DIR


def result_path(root: str | Path, turn: int) -> Path:
    return agent_dir(root) / "results" / f"envelope-{turn}.json"


def scratch_environment(root: str | Path) -> dict[str, str]:
    base = agent_dir(root) / "scratch"
    redirects = {
        "TMPDIR": str(base / "tmp"),
        "PYTHONPYCACHEPREFIX": str(base / "pycache"),
        "PYTEST_ADDOPTS": "-o cache_dir={}".format(base / "pytest_cache"),
        "COVERAGE_FILE": str(base / "coverage"),
        "RUFF_CACHE_DIR": str(base / "ruff"),
        "npm_config_cache": str(base / "npm"),
    }
    for key in ("TMPDIR", "PYTHONPYCACHEPREFIX", "RUFF_CACHE_DIR", "npm_config_cache"):
        Path(redirects[key]).mkdir(parents=True, exist_ok=True)
    (base / "pytest_cache").mkdir(parents=True, exist_ok=True)
    (agent_dir(root) / "home").mkdir(parents=True, exist_ok=True)
    (agent_dir(root) / "results").mkdir(parents=True, exist_ok=True)
    return redirects


def _without_selector(raw: str) -> str:
    if _URI.match(raw):
        raise IsolationRefused("non-local path")
    # OMP selectors follow the local path. Existing local names containing a colon
    # are preserved; otherwise the first selector-looking suffix is removed.
    candidate = Path(os.path.expanduser(raw))
    if candidate.exists():
        return raw
    match = re.match(
        r"^(.*?):(?:raw(?::.*)?|img|conflicts|\d+(?:[-+]\d+)?(?:,.*)?)$", raw
    )
    return match.group(1) if match else raw


def require_path(root: str | Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise IsolationRefused("missing path")
    text = _without_selector(raw.strip())
    if text.startswith("~"):
        candidate = Path(text).expanduser()
    else:
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
    resolved_root = Path(root).resolve()
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise IsolationRefused("path leaves assigned checkout") from exc
    if relative.parts and relative.parts[0] == ".git":
        raise IsolationRefused("Git metadata is broker-owned")
    return resolved


def _uri_scheme(raw: str) -> str | None:
    if not _URI.match(raw):
        return None
    return raw.split("://", 1)[0].lower()


_DELEGATION_TOOLS = frozenset({"agent", "eval", "hub", "task"})
_FILE_TOOLS = frozenset({"edit", "glob", "grep", "lsp", "read", "write"})
_INTERNAL_SCHEMES = frozenset(
    {
        "artifact",
        "issue",
        "local",
        "mcp",
        "memory",
        "omp",
        "pr",
        "rule",
        "skill",
        "xd",
    }
)
_DELEGATION_SCHEMES = frozenset({"agent", "history"})
_GLOB_MAGIC = re.compile(r"[*?\[{]")
_EDIT_HEADER = re.compile(r"^\[([^\]#]+)#([0-9A-Fa-f]{4})\]", re.MULTILINE)


def _admit_uri(root: str | Path, raw: str) -> None:
    scheme = _uri_scheme(raw)
    if scheme is None:
        raise IsolationRefused("non-local path")
    if scheme in _DELEGATION_SCHEMES:
        raise IsolationRefused("delegation is not available")
    if scheme in _INTERNAL_SCHEMES:
        return
    if scheme == "file":
        parsed = urlparse(raw)
        if parsed.netloc and parsed.netloc not in {"localhost", "127.0.0.1"}:
            raise IsolationRefused("non-local path")
        require_path(root, unquote(parsed.path or ""))
        return
    raise IsolationRefused("non-local path")


def _bound_local_or_uri(root: str | Path, raw: object) -> None:
    if not isinstance(raw, str) or not raw.strip():
        raise IsolationRefused("missing path")
    text = raw.strip()
    if _URI.match(text):
        _admit_uri(root, text)
        return
    require_path(root, text)


def _bound_glob_pattern(root: str | Path, pattern: str) -> None:
    if _URI.match(pattern):
        _admit_uri(root, pattern)
        return
    resolved_root = Path(root).resolve()
    prefix: list[str] = []
    for segment in Path(pattern).parts:
        if segment == ".":
            continue
        if _GLOB_MAGIC.search(segment):
            break
        prefix.append(segment)
    if prefix:
        require_path(root, str(Path(*prefix)))
    if not _GLOB_MAGIC.search(pattern):
        require_path(root, pattern)
        return
    try:
        matches = list(resolved_root.glob(pattern))
    except ValueError as exc:
        raise IsolationRefused("path leaves assigned checkout") from exc
    for match in matches:
        try:
            match.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise IsolationRefused("path leaves assigned checkout") from exc


def _payload_path_values(tool: str, payload: Mapping[str, Any]) -> list[object]:
    values: list[object] = []
    for key in (
        "cwd",
        "directory",
        "file",
        "file_path",
        "path",
        "program",
        "notebook_path",
        "repo_path",
        "repository_path",
        "root",
        "root_path",
        "working_directory",
    ):
        if key in payload:
            values.append(payload[key])
    paths = payload.get("paths")
    if isinstance(paths, str):
        values.append(paths)
    elif isinstance(paths, Sequence) and not isinstance(paths, (bytes, str)):
        values.extend(paths)
    if tool == "edit":
        raw = payload.get("input")
        if isinstance(raw, str):
            values.extend(match.group(1) for match in _EDIT_HEADER.finditer(raw))
    return values


def check_tool_input(root: str | Path, tool: str, payload: Mapping[str, Any]) -> None:
    name = tool.lower()
    if name in _DELEGATION_TOOLS:
        raise IsolationRefused("delegation is not available")
    if name == "bash":
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            cwd = str(root)
        require_path(root, cwd)
        return
    values = _payload_path_values(name, payload)
    if not values:
        if name in {"glob", "grep"}:
            values = ["."]
        elif name in _FILE_TOOLS:
            raise IsolationRefused("missing path")
        else:
            return
    globbing = name in {"glob", "grep"}
    for raw in values:
        if globbing and isinstance(raw, str):
            for part in raw.split(";"):
                part = part.strip()
                if part:
                    _bound_glob_pattern(root, part)
        else:
            _bound_local_or_uri(root, raw)


def omp_hook_path() -> Path:
    return Path(__file__).with_name("workspace_isolation_hook.ts")


def _relative_parts(root: str | Path, path: str | Path) -> tuple[Path, tuple[str, ...]]:
    resolved_root = Path(root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    normalized = Path(os.path.abspath(candidate))
    try:
        relative = normalized.relative_to(resolved_root)
    except ValueError as exc:
        raise IsolationRefused("path leaves assigned checkout") from exc
    if not relative.parts:
        raise IsolationRefused("path names the checkout directory")
    return resolved_root, relative.parts


def read_bytes_beneath(root: str | Path, path: str | Path) -> bytes:
    """Read one regular file without following any role-controlled symlink."""
    resolved_root, parts = _relative_parts(root, path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        current = os.open(resolved_root, directory_flags)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, directory_flags | nofollow, dir_fd=current)
            descriptors.append(current)
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise IsolationRefused("role output is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(file_fd)
    except OSError as exc:
        if exc.errno == getattr(os, "ELOOP", 62):
            raise IsolationRefused("role output traverses a symlink") from exc
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_text_beneath(root: str | Path, path: str | Path) -> str:
    return read_bytes_beneath(root, path).decode("utf-8")


def validate_git_marker(checkout: str | Path, common_dir: str | Path) -> None:
    root = Path(checkout).resolve()
    marker = read_text_beneath(root, root / ".git").strip()
    prefix = "gitdir:"
    if not marker.startswith(prefix):
        raise IsolationRefused("ROLE_GIT_BINDING_INVALID")
    raw = marker[len(prefix) :].strip()
    gitdir = Path(raw)
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    resolved_gitdir = gitdir.resolve()
    resolved_common = Path(common_dir).resolve()
    try:
        relative = resolved_gitdir.relative_to(resolved_common)
    except ValueError as exc:
        raise IsolationRefused("ROLE_GIT_BINDING_MISMATCH") from exc
    if len(relative.parts) < 2 or relative.parts[0] != "worktrees":
        raise IsolationRefused("ROLE_GIT_BINDING_MISMATCH")


def claude_settings(root: str | Path) -> str:
    resolved_root = str(Path(root).resolve())
    hook = {
        "type": "command",
        "command": sys.executable,
        "args": [str(Path(__file__).resolve()), "claude-hook", resolved_root],
    }
    settings = {
        "sandbox": {"enabled": False},
        "permissions": {"deny": ["Task", "Agent"]},
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]},
    }

    return json.dumps(settings, sort_keys=True, separators=(",", ":"))


def _safe_subprocess_environment(root: Path) -> dict[str, str]:
    source = os.environ
    safe = {key: source[key] for key in _SAFE_ENV_KEYS if source.get(key)}
    safe.update(scratch_environment(root))
    safe["HOME"] = str(agent_dir(root) / "home")
    safe["PATH"] = f"{root / 'node_modules' / '.bin'}:/usr/local/bin:/usr/bin:/bin"
    safe["PWD"] = str(root)
    return safe


def _docker_environment() -> dict[str, str]:
    source = os.environ
    keys = ("DOCKER_CONTEXT", "DOCKER_HOST", "HOME", "PATH")
    return {key: source[key] for key in keys if source.get(key)}


def _docker_argv(root: Path, cwd: Path, command: str) -> list[str]:
    binary = shutil.which("docker")
    if not binary:
        raise IsolationRefused("AGENT_ISOLATION_UNAVAILABLE:docker")
    argv = [
        binary,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,mode=1777",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{root}:{root}:rw",
        "--workdir",
        str(cwd),
    ]
    dotgit = root / ".git"
    if dotgit.exists():
        argv.extend(("--mount", f"type=bind,src=/dev/null,dst={dotgit},readonly"))
    for key, value in sorted(_safe_subprocess_environment(root).items()):
        argv.extend(("--env", f"{key}={value}"))
    argv.extend((_SANDBOX_IMAGE, "/bin/bash", "--noprofile", "--norc", "-c", command))
    return argv


def run_bash(root_raw: str, cwd_raw: str, encoded_command: str) -> int:
    root = Path(root_raw).resolve()
    cwd = require_path(root, cwd_raw)
    try:
        command = base64.b64decode(encoded_command, validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise IsolationRefused("invalid command envelope") from exc
    completed = subprocess.run(
        _docker_argv(root, cwd, command),
        env=_docker_environment(),
        check=False,
    )
    return int(completed.returncode)


def preflight_sandbox(root: str | Path) -> None:
    resolved = Path(root).resolve()
    docker = shutil.which("docker")
    if not docker:
        raise IsolationRefused("AGENT_ISOLATION_UNAVAILABLE:docker")
    inspected = subprocess.run(
        (docker, "image", "inspect", _SANDBOX_IMAGE),
        env=_docker_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if inspected.returncode:
        adws = Path(__file__).resolve().parents[1]
        dockerfile = adws / "tools" / "role_sandbox.Dockerfile"
        raise IsolationRefused(
            "AGENT_ISOLATION_UNAVAILABLE:image:{}; build with docker build "
            "--tag {} --file {} {}".format(
                _SANDBOX_IMAGE, _SANDBOX_IMAGE, dockerfile, adws
            )
        )
    encoded = base64.b64encode(b"true").decode("ascii")
    try:
        outcome = run_bash(str(resolved), str(resolved), encoded)
    except (IsolationRefused, OSError) as exc:
        raise IsolationRefused(f"AGENT_ISOLATION_UNAVAILABLE:{exc}") from exc
    if outcome != 0:
        raise IsolationRefused(f"AGENT_ISOLATION_UNAVAILABLE:exit-{outcome}")


def _claude_hook(configured_root: str | None = None) -> int:
    try:
        root_raw = (
            configured_root
            or os.environ.get("MAESTRO_ROLE_ROOT")
            or os.environ.get("CLAUDE_PROJECT_DIR", "")
        )
        if not root_raw:
            raise IsolationRefused("isolation configuration missing")
        root = str(Path(root_raw).resolve())
        event = json.load(sys.stdin)
        tool = str(event.get("tool_name") or "")
        payload = event.get("tool_input")
        if not isinstance(payload, Mapping):
            raise IsolationRefused("tool input is not a mapping")
        check_tool_input(root, tool, payload)
        if tool.lower() != "bash":
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                            "permissionDecisionReason": "Bounded role capability",
                        }
                    },
                    sort_keys=True,
                )
            )
            return 0
        command = payload.get("command")
        if not isinstance(command, str):
            raise IsolationRefused("bash command missing")
        cwd = str(payload.get("cwd") or root)
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        wrapped = " ".join(
            shlex.quote(value)
            for value in (
                sys.executable,
                str(Path(__file__).resolve()),
                "run-bash",
                root,
                cwd,
                encoded,
            )
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": "Runs inside the role container",
                        "updatedInput": {**payload, "command": wrapped, "cwd": root},
                    }
                },
                sort_keys=True,
            )
        )

    except (IsolationRefused, OSError, ValueError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"MAESTRO_WORKTREE_BOUNDARY:{exc}",
                    }
                },
                sort_keys=True,
            )
        )
    return 0


def _check_tool_cli(root: str, tool: str, encoded_payload: str) -> int:
    try:
        decoded = base64.b64decode(encoded_payload, validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, UnicodeError) as exc:
        raise IsolationRefused("invalid tool envelope") from exc
    if not isinstance(payload, Mapping):
        raise IsolationRefused("tool input is not a mapping")
    check_tool_input(root, tool, payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args == ["claude-hook"]:
        return _claude_hook()
    if len(args) == 2 and args[0] == "claude-hook":
        return _claude_hook(args[1])
    if len(args) == 4 and args[0] == "run-bash":
        return run_bash(args[1], args[2], args[3])
    if len(args) == 4 and args[0] == "check-tool":
        try:
            return _check_tool_cli(args[1], args[2], args[3])
        except IsolationRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2
    raise SystemExit(
        "usage: workspace_isolation.py claude-hook [ROOT] | "
        "run-bash ROOT CWD BASE64 | check-tool ROOT TOOL BASE64_JSON"
    )


if __name__ == "__main__":
    raise SystemExit(main())
