"""Non-destructive Maestro dashboard launcher.

Starts or reuses the Bun API and Next.js dashboard. Never kills an unknown
listener. Factory parent detaches this process; readiness wait lives here.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

DEFAULT_API_PORT = 4600
DEFAULT_UI_PORT = 4317
HEALTH_PATH = "/api/health"
SOURCES_PATH = "/api/sources"
RUNS_PATH = "/runs"
TOKEN_ENV = "MAESTRO_DASHBOARD_TOKEN"
RUNTIME_ENV = "MAESTRO_DASHBOARD_RUNTIME"


def dashboard_root(launcher_file: Path | None = None) -> Path:
    here = Path(launcher_file or __file__).resolve()
    return here.parent.parent


def visualizer_root(launcher_file: Path | None = None) -> Path:
    return dashboard_root(launcher_file).parent / "visualizer"


def default_runtime_root(api_port: int, ui_port: int) -> Path:
    override = os.environ.get(RUNTIME_ENV)
    if override:
        base = Path(override)
    else:
        cache = os.environ.get("XDG_CACHE_HOME")
        base = Path(cache) if cache else Path.home() / ".cache"
        base = base / "maestro-dashboard"
    return base / f"{api_port}-{ui_port}"


def warn(detail: str) -> None:
    sys.stderr.write("dashboard: {0}\n".format(detail))


@dataclass(frozen=True)
class Fingerprint:
    argv: tuple[str, ...]
    cwd: str
    start_identity: str


@dataclass(frozen=True)
class OwnerRecord:
    pid: int
    argv: tuple[str, ...]
    cwd: str
    start_identity: str
    port: int
    kind: str

    def fingerprint(self) -> Fingerprint:
        return Fingerprint(self.argv, self.cwd, self.start_identity)


@dataclass(frozen=True)
class LaunchSpec:
    ledger: Path
    repository: Path
    api_port: int = DEFAULT_API_PORT
    ui_port: int = DEFAULT_UI_PORT
    open_browser: bool = True
    timeout_s: float = 30.0
    poll_s: float = 0.25
    launcher_file: Path | None = None


class Host:
    def listeners(self, port: int) -> list[int]:
        raise NotImplementedError

    def pid_alive(self, pid: int) -> bool:
        raise NotImplementedError

    def fingerprint(self, pid: int) -> Fingerprint | None:
        raise NotImplementedError

    def spawn(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> int:
        raise NotImplementedError

    def terminate(self, pid: int) -> None:
        raise NotImplementedError

    def kill(self, pid: int) -> None:
        raise NotImplementedError

    def http_code(self, url: str) -> int | None:
        raise NotImplementedError

    def http_json(self, url: str) -> Any:
        raise NotImplementedError

    def open_browser(self, url: str) -> None:
        raise NotImplementedError

    def which(self, name: str) -> str | None:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError

    def path_is_file(self, path: Path) -> bool:
        raise NotImplementedError

    def warn(self, detail: str) -> None:
        warn(detail)


class RealHost(Host):
    def listeners(self, port: int) -> list[int]:
        result = subprocess.run(
            ["lsof", "-nP", "-t", "-iTCP:{0}".format(port), "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
        pids: list[int] = []
        for token in result.stdout.split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        return sorted(set(pids))

    def pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def fingerprint(self, pid: int) -> Fingerprint | None:
        if not self.pid_alive(pid):
            return None
        argv = _live_argv(pid)
        cwd = _live_cwd(pid)
        identity = _live_start_identity(pid)
        if argv is None or cwd is None or identity is None:
            return None
        return Fingerprint(argv=argv, cwd=cwd, start_identity=identity)

    def spawn(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            handle.close()
        return proc.pid

    def terminate(self, pid: int) -> None:
        try:
            os.kill(pid, 15)
        except OSError:
            return

    def kill(self, pid: int) -> None:
        try:
            os.kill(pid, 9)
        except OSError:
            return

    def http_code(self, url: str) -> int | None:
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=3) as response:
                return int(response.getcode())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def http_json(self, url: str) -> Any:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def open_browser(self, url: str) -> None:
        webbrowser.open(url)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def path_is_file(self, path: Path) -> bool:
        return path.is_file()


def _live_argv(pid: int) -> tuple[str, ...] | None:
    proc = Path("/proc/{0}/cmdline".format(pid))
    if proc.is_file():
        try:
            raw = proc.read_bytes()
        except OSError:
            return None
        parts = tuple(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
        return parts or None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-www", "-o", "args="],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    if not text:
        return None
    try:
        return tuple(shlex.split(text))
    except ValueError:
        return (text,)


def _live_cwd(pid: int) -> str | None:
    proc = Path("/proc/{0}/cwd".format(pid))
    if proc.exists() or proc.is_symlink():
        try:
            return str(proc.resolve())
        except OSError:
            pass
    result = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _live_start_identity(pid: int) -> str | None:
    stat_path = Path("/proc/{0}/stat".format(pid))
    if stat_path.is_file():
        try:
            text = stat_path.read_text(encoding="utf-8")
        except OSError:
            return None
        close = text.rfind(")")
        if close == -1:
            return None
        fields = text[close + 2 :].split()
        if len(fields) < 20:
            return None
        return "starttime:{0}".format(fields[19])
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    identity = result.stdout.strip()
    return identity or None


def matches_ownership(host: Host, pid: int, recorded: OwnerRecord) -> bool:
    if pid != recorded.pid:
        return False
    if not host.pid_alive(pid):
        return False
    live = host.fingerprint(pid)
    if live is None:
        return False
    return (
        live.argv == recorded.argv
        and live.cwd == recorded.cwd
        and live.start_identity == recorded.start_identity
    )


def read_owner(path: Path) -> OwnerRecord | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    try:
        argv = payload["argv"]
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return None
        return OwnerRecord(
            pid=int(payload["pid"]),
            argv=tuple(argv),
            cwd=str(payload["cwd"]),
            start_identity=str(payload["start_identity"]),
            port=int(payload["port"]),
            kind=str(payload["kind"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_owner(path: Path, record: OwnerRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "pid": record.pid,
            "argv": list(record.argv),
            "cwd": record.cwd,
            "start_identity": record.start_identity,
            "port": record.port,
            "kind": record.kind,
        },
        indent=2,
        sort_keys=True,
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(path)


def clear_owner(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def canonical_ledger_path(path: Path) -> str:
    return str(Path(path).expanduser().resolve())


def sources_include_ledger(payload: Any, ledger: Path) -> bool:
    want = canonical_ledger_path(ledger)
    if not isinstance(payload, list):
        return False
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            got = str(Path(raw).expanduser().resolve())
        except OSError:
            got = raw
        if got == want:
            return True
    return False


def browser_url(ui_port: int) -> str:
    return "http://localhost:{0}{1}".format(ui_port, RUNS_PATH)


def api_url(api_port: int, path: str) -> str:
    return "http://127.0.0.1:{0}{1}".format(api_port, path)


def ui_url(ui_port: int, path: str) -> str:
    return "http://127.0.0.1:{0}{1}".format(ui_port, path)


def api_argv(bun: str, spec: LaunchSpec) -> list[str]:
    server = visualizer_root(spec.launcher_file) / "server" / "index.ts"
    return [bun, "run", str(server)]


def ui_argv(bun: str, spec: LaunchSpec) -> list[str]:
    nxt = dashboard_root(spec.launcher_file) / "node_modules" / ".bin" / "next"
    return [bun, str(nxt), "dev", "-p", str(spec.ui_port)]


def _stop_owned(host: Host, record: OwnerRecord) -> bool:
    pid = record.pid
    if not matches_ownership(host, pid, record):
        return False
    host.terminate(pid)
    for _ in range(20):
        if not host.pid_alive(pid):
            return True
        host.sleep(0.05)
    if not matches_ownership(host, pid, record):
        return False
    host.kill(pid)
    host.sleep(0.05)
    return not host.pid_alive(pid)


def _owned_listener(
    host: Host, port: int, meta: Path
) -> tuple[OwnerRecord | None, list[int]]:
    recorded = read_owner(meta)
    listeners = host.listeners(port)
    if recorded is not None and matches_ownership(host, recorded.pid, recorded):
        return recorded, listeners
    if recorded is not None:
        clear_owner(meta)
    return None, listeners


def _spawn_owned(
    host: Host,
    *,
    kind: str,
    port: int,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    meta: Path,
    log_path: Path,
) -> OwnerRecord | None:
    try:
        pid = host.spawn(argv, cwd, env, log_path)
    except OSError as exc:
        host.warn("failed to spawn {0}: {1}; skipping".format(kind, exc))
        return None
    live = host.fingerprint(pid)
    if live is None:
        record = OwnerRecord(
            pid=pid,
            argv=tuple(argv),
            cwd=str(cwd),
            start_identity="pid:{0}".format(pid),
            port=port,
            kind=kind,
        )
    else:
        record = OwnerRecord(
            pid=pid,
            argv=live.argv,
            cwd=live.cwd,
            start_identity=live.start_identity,
            port=port,
            kind=kind,
        )
    write_owner(meta, record)
    return record


def ensure_api(
    spec: LaunchSpec,
    host: Host,
    runtime: Path,
    bun: str,
) -> bool:
    meta = runtime / "api.json"
    log_path = runtime / "api.log"
    argv = api_argv(bun, spec)
    cwd = spec.repository
    env = os.environ.copy()
    env["PORT"] = str(spec.api_port)
    env[TOKEN_ENV] = "api:{0}:{1}".format(
        spec.api_port, canonical_ledger_path(spec.ledger)
    )
    owned, listeners = _owned_listener(host, spec.api_port, meta)
    if owned is not None:
        if listeners and owned.pid not in listeners:
            host.warn(
                "port {0} occupied by pid(s) {1}; skipping API".format(
                    spec.api_port, " ".join(str(pid) for pid in listeners)
                )
            )
            return False
        health = host.http_code(api_url(spec.api_port, HEALTH_PATH))
        if health == 200:
            try:
                payload = host.http_json(api_url(spec.api_port, SOURCES_PATH))
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ValueError,
                json.JSONDecodeError,
            ):
                payload = None
            if payload is not None and not sources_include_ledger(
                payload, spec.ledger
            ):
                if not _stop_owned(host, owned):
                    host.warn(
                        "owned API on :{0} is stale but no longer matches ownership; skipping".format(
                            spec.api_port
                        )
                    )
                    return False
                clear_owner(meta)
                listeners = host.listeners(spec.api_port)
            else:
                return True
        else:
            return True
    if listeners:
        host.warn(
            "port {0} occupied by pid(s) {1}; skipping API".format(
                spec.api_port, " ".join(str(pid) for pid in listeners)
            )
        )
        return False
    return _spawn_owned(
        host,
        kind="api",
        port=spec.api_port,
        argv=argv,
        cwd=cwd,
        env=env,
        meta=meta,
        log_path=log_path,
    ) is not None


def ensure_ui(
    spec: LaunchSpec,
    host: Host,
    runtime: Path,
    bun: str,
) -> bool:
    nxt = dashboard_root(spec.launcher_file) / "node_modules" / ".bin" / "next"
    if not host.path_is_file(nxt):
        host.warn("Next.js binary missing at {0}; skipping UI".format(nxt))
        return False
    meta = runtime / "ui.json"
    log_path = runtime / "ui.log"
    argv = ui_argv(bun, spec)
    cwd = dashboard_root(spec.launcher_file)
    env = os.environ.copy()
    env["MAESTRO_API_PORT"] = str(spec.api_port)
    env[TOKEN_ENV] = "ui:{0}".format(spec.ui_port)
    owned, listeners = _owned_listener(host, spec.ui_port, meta)
    if owned is not None:
        if listeners and owned.pid not in listeners:
            host.warn(
                "port {0} occupied by pid(s) {1}; skipping UI".format(
                    spec.ui_port, " ".join(str(pid) for pid in listeners)
                )
            )
            return False
        return True
    if listeners:
        host.warn(
            "port {0} occupied by pid(s) {1}; skipping UI".format(
                spec.ui_port, " ".join(str(pid) for pid in listeners)
            )
        )
        return False
    return _spawn_owned(
        host,
        kind="ui",
        port=spec.ui_port,
        argv=argv,
        cwd=cwd,
        env=env,
        meta=meta,
        log_path=log_path,
    ) is not None


def wait_ready(spec: LaunchSpec, host: Host) -> bool:
    deadline_steps = max(1, int(spec.timeout_s / spec.poll_s)) if spec.poll_s else 1
    last_reason = "not ready"
    for _ in range(deadline_steps):
        health = host.http_code(api_url(spec.api_port, HEALTH_PATH))
        if health != 200:
            last_reason = "API /api/health status {0}".format(health)
            host.sleep(spec.poll_s)
            continue
        try:
            payload = host.http_json(api_url(spec.api_port, SOURCES_PATH))
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            last_reason = "API /api/sources {0}".format(exc)
            host.sleep(spec.poll_s)
            continue
        if not sources_include_ledger(payload, spec.ledger):
            last_reason = "API /api/sources missing {0}".format(
                canonical_ledger_path(spec.ledger)
            )
            host.sleep(spec.poll_s)
            continue
        runs = host.http_code(ui_url(spec.ui_port, RUNS_PATH))
        if runs != 200:
            last_reason = "UI /runs status {0}".format(runs)
            host.sleep(spec.poll_s)
            continue
        return True
    host.warn("{0}; not opening browser".format(last_reason))
    return False


def run_autoload(spec: LaunchSpec, host: Host, runtime: Path) -> bool:
    bun = host.which("bun")
    viz = visualizer_root(spec.launcher_file) / "server" / "index.ts"
    runtime.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(runtime / "lock"):
        if not bun:
            host.warn("bun is not on PATH; skipping spawn")
        elif not host.path_is_file(viz):
            host.warn("visualizer API missing at {0}; skipping spawn".format(viz))
        else:
            ensure_api(spec, host, runtime, bun)
            ensure_ui(spec, host, runtime, bun)
        if not wait_ready(spec, host):
            return False
        if spec.open_browser:
            url = browser_url(spec.ui_port)
            try:
                host.open_browser(url)
            except OSError as exc:
                host.warn("failed to open {0}: {1}".format(url, exc))
                return False
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maestro-dashboard")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT)
    parser.add_argument("--open", dest="open_browser", action="store_true", default=True)
    parser.add_argument("--no-open", dest="open_browser", action="store_false")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def spec_from_args(
    args: argparse.Namespace, *, repository: Path | None = None
) -> LaunchSpec:
    return LaunchSpec(
        ledger=Path(args.ledger),
        repository=(repository or Path.cwd()).resolve(),
        api_port=args.api_port,
        ui_port=args.ui_port,
        open_browser=bool(args.open_browser),
        timeout_s=float(args.timeout),
        launcher_file=Path(__file__),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    runtime: Path | None = None,
    repository: Path | None = None,
) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    spec = spec_from_args(args, repository=repository)
    active_host = host or RealHost()
    root = runtime or default_runtime_root(spec.api_port, spec.ui_port)
    try:
        run_autoload(spec, active_host, root)
    except Exception as exc:
        active_host.warn("{0}: {1}; skipping".format(type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
