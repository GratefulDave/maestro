"""Dashboard launcher ownership, stale API, readiness, and browser URL. Fakes only."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BIN = (
    Path(__file__).resolve().parents[3]
    / "apps"
    / "dashboard"
    / "bin"
)
_LAUNCHER_PY = BIN / "launcher.py"
_LAUNCHER_SKIP = "source dashboard launcher absent: {0}".format(_LAUNCHER_PY)

if _LAUNCHER_PY.is_file():
    sys.path.insert(0, str(BIN))
    import launcher  # noqa: E402
else:
    launcher = None

_HostBase = object if launcher is None else launcher.Host


class FakeHost(_HostBase):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.processes: dict[int, dict] = {}
        self.listeners_map: dict[int, list[int]] = {}
        self.http_codes: dict[str, int] = {}
        self.http_bodies: dict[str, object] = {}
        self.spawned: list[int] = []
        self.signals: list[tuple[str, int]] = []
        self.opened: list[str] = []
        self.warnings: list[str] = []
        self.next_pid = 1000
        self.files: set[str] = set()
        self.bun = "/fake/bun"

    def listeners(self, port: int) -> list[int]:
        with self._lock:
            return list(self.listeners_map.get(port, []))

    def pid_alive(self, pid: int) -> bool:
        with self._lock:
            proc = self.processes.get(pid)
            return bool(proc and proc["alive"])

    def fingerprint(self, pid: int) -> launcher.Fingerprint | None:
        with self._lock:
            proc = self.processes.get(pid)
            if not proc or not proc["alive"]:
                return None
            return launcher.Fingerprint(
                argv=tuple(proc["argv"]),
                cwd=proc["cwd"],
                start_identity=proc["start_identity"],
            )

    def spawn(self, argv, cwd, env, log_path) -> int:
        with self._lock:
            pid = self.next_pid
            self.next_pid += 1
            argv_t = tuple(argv)
            record = {
                "argv": argv_t,
                "cwd": str(cwd),
                "env": dict(env),
                "alive": True,
                "start_identity": "spawn-{0}".format(pid),
                "log_path": str(log_path),
            }
            port = None
            if env.get("PORT"):
                port = int(env["PORT"])
            elif "-p" in argv:
                port = int(argv[list(argv).index("-p") + 1])
            if port is not None:
                record["port"] = port
                self.listeners_map.setdefault(port, []).append(pid)
            self.processes[pid] = record
            self.spawned.append(pid)
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_bytes(b"")
            return pid

    def terminate(self, pid: int) -> None:
        self.signals.append(("TERM", pid))
        self._reap(pid)

    def kill(self, pid: int) -> None:
        self.signals.append(("KILL", pid))
        self._reap(pid)

    def _reap(self, pid: int) -> None:
        with self._lock:
            proc = self.processes.get(pid)
            if not proc:
                return
            proc["alive"] = False
            port = proc.get("port")
            if port is not None:
                held = self.listeners_map.get(port, [])
                self.listeners_map[port] = [item for item in held if item != pid]

    def http_code(self, url: str) -> int | None:
        return self.http_codes.get(url)

    def http_json(self, url: str):
        if url not in self.http_bodies:
            raise OSError("no fake body for {0}".format(url))
        return self.http_bodies[url]

    def open_browser(self, url: str) -> None:
        self.opened.append(url)

    def which(self, name: str) -> str | None:
        if name == "bun":
            return self.bun
        return None

    def sleep(self, seconds: float) -> None:
        return None

    def path_is_file(self, path: Path) -> bool:
        return True

    def warn(self, detail: str) -> None:
        self.warnings.append(detail)
        launcher.warn(detail)

    def add_unrelated(self, port: int, pid: int, argv=("other",), cwd="/else") -> None:
        with self._lock:
            self.processes[pid] = {
                "argv": tuple(argv),
                "cwd": cwd,
                "env": {},
                "alive": True,
                "start_identity": "foreign-{0}".format(pid),
                "port": port,
            }
            self.listeners_map.setdefault(port, []).append(pid)


def _spec(tmp: Path, **kwargs) -> launcher.LaunchSpec:
    ledger = tmp / "lifecycle.sqlite3"
    ledger.write_bytes(b"")
    repo = tmp / "repo"
    repo.mkdir(exist_ok=True)
    values = dict(
        ledger=ledger,
        repository=repo,
        api_port=4600,
        ui_port=4317,
        open_browser=True,
        timeout_s=0.05,
        poll_s=0.01,
        launcher_file=BIN / "launcher.py",
    )
    values.update(kwargs)
    return launcher.LaunchSpec(**values)


def _ready(host: FakeHost, spec: launcher.LaunchSpec, ledger: Path) -> None:
    host.http_codes[launcher.api_url(spec.api_port, "/api/health")] = 200
    host.http_bodies[launcher.api_url(spec.api_port, "/api/sources")] = [
        {"path": str(ledger.resolve()), "kind": "maestro"}
    ]
    host.http_codes[launcher.ui_url(spec.ui_port, "/runs")] = 200


@unittest.skipUnless(_LAUNCHER_PY.is_file(), _LAUNCHER_SKIP)
class DashboardLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.host = FakeHost()
        self.spec = _spec(self.root)
        _ready(self.host, self.spec, self.spec.ledger)
        self.addCleanup(self.tmp.cleanup)

    def test_reuses_healthy_owned_process(self) -> None:
        self.assertTrue(launcher.run_autoload(self.spec, self.host, self.runtime))
        first = list(self.host.spawned)
        self.assertEqual(len(first), 2)
        self.assertTrue(launcher.run_autoload(self.spec, self.host, self.runtime))
        self.assertEqual(self.host.spawned, first)
        self.assertEqual(self.host.signals, [])
        self.assertEqual(
            self.host.opened,
            [
                "http://localhost:4317/runs",
                "http://localhost:4317/runs",
            ],
        )

    def test_stale_dead_pid_is_cleared_and_respawned(self) -> None:
        self.assertTrue(launcher.run_autoload(self.spec, self.host, self.runtime))
        api_pid = self.host.spawned[0]
        self.host._reap(api_pid)
        meta = json.loads((self.runtime / "api.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["pid"], api_pid)
        self.host.opened.clear()
        self.assertTrue(launcher.run_autoload(self.spec, self.host, self.runtime))
        new_meta = json.loads((self.runtime / "api.json").read_text(encoding="utf-8"))
        self.assertNotEqual(new_meta["pid"], api_pid)
        self.assertNotIn(("TERM", api_pid), self.host.signals)
        self.assertNotIn(("KILL", api_pid), self.host.signals)

    def test_unrelated_port_is_not_killed(self) -> None:
        self.host.add_unrelated(4600, 99)
        self.host.add_unrelated(4317, 98)
        ok = launcher.run_autoload(self.spec, self.host, self.runtime)
        self.assertTrue(ok)
        self.assertEqual(self.host.spawned, [])
        self.assertEqual(self.host.signals, [])
        self.assertTrue(any("occupied" in item for item in self.host.warnings))
        self.assertEqual(self.host.opened, ["http://localhost:4317/runs"])

    def test_unrelated_api_missing_source_skips_open(self) -> None:
        self.host.add_unrelated(4600, 99)
        self.host.add_unrelated(4317, 98)
        self.host.http_bodies[launcher.api_url(4600, "/api/sources")] = [
            {"path": "/other/lifecycle.sqlite3", "kind": "maestro"}
        ]
        ok = launcher.run_autoload(self.spec, self.host, self.runtime)
        self.assertFalse(ok)
        self.assertEqual(self.host.signals, [])
        self.assertEqual(self.host.spawned, [])
        self.assertEqual(self.host.opened, [])
        self.assertTrue(any("missing" in item for item in self.host.warnings))

    def test_owned_stale_source_restarts_only_owned(self) -> None:
        self.assertTrue(launcher.run_autoload(self.spec, self.host, self.runtime))
        api_pid = self.host.spawned[0]
        self.host.opened.clear()
        self.host.http_bodies[launcher.api_url(4600, "/api/sources")] = [
            {"path": "/stale/lifecycle.sqlite3"}
        ]
        ok = launcher.run_autoload(self.spec, self.host, self.runtime)
        self.assertFalse(ok)
        self.assertIn(("TERM", api_pid), self.host.signals)
        self.assertEqual(len(self.host.spawned), 3)
        self.assertNotEqual(self.host.spawned[-1], api_pid)
        self.assertEqual(self.host.opened, [])

    def test_concurrent_calls_serialize_to_one_spawn_pair(self) -> None:
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                launcher.run_autoload(self.spec, self.host, self.runtime)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.host.spawned), 2)
        self.assertEqual(self.host.opened, ["http://localhost:4317/runs"] * 2)

    def test_no_open_flag_skips_browser(self) -> None:
        spec = _spec(self.root, open_browser=False)
        _ready(self.host, spec, spec.ledger)
        self.assertTrue(launcher.run_autoload(spec, self.host, self.runtime))
        self.assertEqual(self.host.opened, [])

    def test_browser_url_is_localhost_runs(self) -> None:
        self.assertEqual(
            launcher.browser_url(4317), "http://localhost:4317/runs"
        )
        self.assertTrue(launcher.run_autoload(self.spec, self.host, self.runtime))
        self.assertEqual(self.host.opened, ["http://localhost:4317/runs"])

    def test_sources_include_canonical_ledger(self) -> None:
        ledger = self.spec.ledger
        self.assertTrue(
            launcher.sources_include_ledger(
                [{"path": str(ledger)}], ledger
            )
        )
        self.assertFalse(
            launcher.sources_include_ledger(
                [{"path": str(self.root / "other.sqlite3")}], ledger
            )
        )


if __name__ == "__main__":
    unittest.main()
