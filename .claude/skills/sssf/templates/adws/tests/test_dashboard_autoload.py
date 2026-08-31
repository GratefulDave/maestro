"""Dashboard autoload: config, start/resume placement, detached spawn, fail-open."""

from __future__ import annotations

import argparse
import importlib.util
import os
import tempfile
import sys
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

import maestro
from adw_modules import dashboard_autoload as autoload
from adw_modules import scheduler_types as st


def _role_routes() -> dict[str, dict[str, str]]:
    return {
        "tester": {"route": "omp", "profile": "grok-maestro"},
        "test-reviewer": {"route": "omp", "profile": "grok-maestro"},
        "builder": {"route": "omp", "profile": "grok-maestro"},
        "code-reviewer": {"route": "omp", "profile": "grok-maestro"},
        "integration-reviewer": {"route": "omp", "profile": "grok-maestro"},
    }


def _write_config(path: Path, extra: dict | None = None) -> None:
    payload = {
        "schema": "maestro-config.v1",
        "runtime_state_root": "/tmp/maestro-state",
        "role_routes": _role_routes(),
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")


_INSTALL_PY = Path(__file__).resolve().parents[3] / "scripts" / "install.py"
_INSTALL_SKIP = "source installer absent: {0}".format(_INSTALL_PY)


def _load_install():
    if not _INSTALL_PY.is_file():
        raise unittest.SkipTest(_INSTALL_SKIP)
    spec = importlib.util.spec_from_file_location("sssf_install", _INSTALL_PY)
    if spec is None or spec.loader is None:
        raise RuntimeError(str(_INSTALL_PY))
    module = importlib.util.module_from_spec(spec)
    sys.modules["sssf_install"] = module
    spec.loader.exec_module(module)
    return module


def _bind_tuple(layout: dict) -> tuple:
    runtime = mock.Mock()
    runtime.ledger_path.return_value = Path("/runtime/lifecycle.sqlite3")
    runtime.path = Path("/runtime")
    store = mock.Mock()
    store.active_projection.return_value = ()
    row = {
        "target_repository_root": "/product",
        "target_main_ref": "refs/heads/main",
        "plan_revision": 1,
    }
    target = SimpleNamespace(
        target_repository_root="/product",
        target_main_ref="refs/heads/main",
    )
    compiled = SimpleNamespace(lanes=())
    return layout, runtime, store, row, target, compiled


class DashboardConfigTest(unittest.TestCase):
    def test_missing_dashboard_normalizes_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = repo / "adws" / "maestro.config.yaml"
            _write_config(config)
            loaded = maestro._load_maestro_config(repo, config)
            self.assertIsNone(loaded["dashboard"])

    def test_valid_stanza_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = repo / "adws" / "maestro.config.yaml"
            _write_config(
                config,
                {
                    "dashboard": {
                        "enabled": True,
                        "launcher": "/abs/maestro-dashboard",
                        "api_port": 4600,
                        "ui_port": 4317,
                        "open": False,
                    }
                },
            )
            loaded = maestro._load_maestro_config(repo, config)
            self.assertEqual(
                dict(loaded["dashboard"]),
                {
                    "enabled": True,
                    "launcher": "/abs/maestro-dashboard",
                    "api_port": 4600,
                    "ui_port": 4317,
                    "open": False,
                },
            )

    def test_defaults_fill_optional_fields(self) -> None:
        canonical = maestro._canonical_dashboard({"enabled": True})
        self.assertIsNotNone(canonical)
        assert canonical is not None
        self.assertEqual(canonical["enabled"], True)
        self.assertIsNone(canonical["launcher"])
        self.assertEqual(canonical["api_port"], 4600)
        self.assertEqual(canonical["ui_port"], 4317)
        self.assertEqual(canonical["open"], True)
    def test_invalid_types_are_refused(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard("yes")
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard({"enabled": "true"})
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard({"api_port": "4600"})
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard({"api_port": True})
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard({"ui_port": 70000})
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard({"open": 1})
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard({"extra": True})

    def test_empty_launcher_is_refused_at_load(self) -> None:
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._canonical_dashboard({"launcher": "  "})


class DashboardSpawnTest(unittest.TestCase):
    def test_detached_argv_env_cwd_and_nonblocking(self) -> None:
        recorded: dict = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                recorded["argv"] = argv
                recorded["kwargs"] = kwargs
                self.pid = 7

            def wait(self):
                raise AssertionError("must not wait")

            def communicate(self):
                raise AssertionError("must not communicate")

        cfg = {
            "enabled": True,
            "launcher": "/abs/maestro-dashboard",
            "api_port": 4600,
            "ui_port": 4317,
            "open": True,
        }
        with mock.patch.object(
            autoload, "registry_path", return_value=Path("/reg.json")
        ):
            autoload.spawn_dashboard_launcher(
                cfg,
                repository=Path("/product"),
                ledger=Path("/runtime/lifecycle.sqlite3"),
                popen=FakePopen,
            )
        self.assertEqual(
            recorded["argv"],
            [
                "/abs/maestro-dashboard",
                "--ledger",
                str(Path("/runtime/lifecycle.sqlite3").resolve()),
                "--api-port",
                "4600",
                "--ui-port",
                "4317",
                "--open",
            ],
        )
        kwargs = recorded["kwargs"]
        self.assertEqual(kwargs["cwd"], str(Path("/product").resolve()))
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdin"], autoload.subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], autoload.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], autoload.subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["MAESTRO_REGISTRY"], "/reg.json")
        self.assertEqual(kwargs["env"]["MAESTRO_API_PORT"], "4600")
        self.assertEqual(kwargs["env"]["MAESTRO_UI_PORT"], "4317")
    def test_missing_launcher_fails_open(self) -> None:
        calls: list = []
        stderr = StringIO()
        cfg = {
            "enabled": True,
            "launcher": "/missing/maestro-dashboard",
            "api_port": 4600,
            "ui_port": 4317,
            "open": True,
        }
        with mock.patch("sys.stderr", stderr):
            autoload.maybe_autoload_dashboard(
                {"dashboard": cfg},
                repository=Path("/product"),
                ledger=Path("/ledger.sqlite3"),
                popen=lambda *a, **k: calls.append((a, k)),
            )
        self.assertEqual(calls, [])
        self.assertIn("invalid launcher", stderr.getvalue())
        self.assertIn("continuing", stderr.getvalue())

    def test_disabled_or_missing_does_not_spawn(self) -> None:
        calls: list = []
        autoload.maybe_autoload_dashboard(
            {},
            repository=Path("/product"),
            ledger=Path("/ledger.sqlite3"),
            popen=lambda *a, **k: calls.append(1),
        )
        autoload.maybe_autoload_dashboard(
            {"dashboard": {"enabled": False, "launcher": "/x", "api_port": 1, "ui_port": 2, "open": True}},
            repository=Path("/product"),
            ledger=Path("/ledger.sqlite3"),
            popen=lambda *a, **k: calls.append(1),
        )
        self.assertEqual(calls, [])

    def test_unexecutable_launcher_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "maestro-dashboard"
            launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            os.chmod(launcher, 0o644)
            stderr = StringIO()
            calls: list = []
            with mock.patch("sys.stderr", stderr):
                autoload.maybe_autoload_dashboard(
                    {
                        "dashboard": {
                            "enabled": True,
                            "launcher": str(launcher),
                            "api_port": 4600,
                            "ui_port": 4317,
                            "open": True,
                        }
                    },
                    repository=Path("/product"),
                    ledger=Path("/ledger.sqlite3"),
                    popen=lambda *a, **k: calls.append(1),
                )
            self.assertEqual(calls, [])
            self.assertIn("invalid launcher", stderr.getvalue())


class DashboardCallPlacementTest(unittest.TestCase):

    def test_start_autoloads_after_register_before_scheduler(self) -> None:
        order: list[str] = []

        def register(*_a, **_k):
            order.append("register")

        def load(*_a, **_k):
            order.append("autoload")

        scheduler_run = []

        class ImmediateScheduler:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                order.append("scheduler")

            def run(self) -> st.RunStatus:
                scheduler_run.append("run")
                return st.RunStatus.WAITING

        runtime = mock.Mock()
        runtime.ledger_path.return_value = Path("/runtime/lifecycle.sqlite3")
        runtime.path = Path("/runtime")
        store = mock.Mock()
        target = SimpleNamespace(
            target_repository_root="/product",
            target_main_ref="refs/heads/main",
        )
        compiled = SimpleNamespace(lanes=(SimpleNamespace(lane_id="lane-a"),))
        args = argparse.Namespace(
            plan="plan.json",
            repo="/product",
            main_ref="refs/heads/main",
            run_id="run-live",
        )
        with (
            mock.patch("sys.stdout", StringIO()),
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=Path("/deploy/maestro.py")
            ),
            mock.patch.object(maestro, "_load_deployment_config", return_value={}),
            mock.patch.object(maestro, "require_deployment"),
            mock.patch.object(maestro, "_open_runtime", return_value=runtime),
            mock.patch.object(maestro, "_compile_plan", return_value=compiled),
            mock.patch.object(
                maestro.gitpub, "bind_target_worktree", return_value=target
            ),
            mock.patch.object(maestro, "_open_store", return_value=store),
            mock.patch.object(maestro, "create_factory_run", return_value=target),
            mock.patch.object(maestro, "target_from_binding", return_value=target),
            mock.patch.object(maestro, "register_installation", side_effect=register),
            mock.patch.object(maestro, "maybe_autoload_dashboard", side_effect=load),
            mock.patch.object(maestro, "_actor_for", return_value=mock.Mock()),
            mock.patch.object(maestro, "FactoryScheduler", ImmediateScheduler),
        ):
            self.assertEqual(maestro._run_start(args), 0)
        self.assertEqual(order, ["register", "autoload", "scheduler"])
        self.assertEqual(scheduler_run, ["run"])

    def test_resume_autoloads_after_bind_amend_and_status_do_not(self) -> None:
        calls: list[str] = []

        def load(*_a, **_k):
            calls.append("autoload")

        layout = {"dashboard": {"enabled": True}}
        bound = _bind_tuple(layout)

        class ImmediateScheduler:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def run(self) -> st.RunStatus:
                return st.RunStatus.WAITING

            def resume_waiting(self) -> None:
                return None

            def status(self) -> st.RunStatus:
                return st.RunStatus.WAITING

        with (
            mock.patch("sys.stdout", StringIO()),
            mock.patch.object(maestro, "_bind_existing_run", return_value=bound),
            mock.patch.object(maestro, "maybe_autoload_dashboard", side_effect=load),
            mock.patch.object(maestro, "_actor_for", return_value=mock.Mock()),
            mock.patch.object(maestro, "FactoryScheduler", ImmediateScheduler),
            mock.patch.object(maestro, "_compile_plan", return_value=bound[5]),
            mock.patch.object(maestro, "apply_factory_amendment"),
            mock.patch.object(maestro.gitpub, "revalidate_binding"),
        ):
            self.assertEqual(
                maestro._run_resume(argparse.Namespace(run_id="run-1")), 0
            )
            self.assertEqual(calls, ["autoload"])
            calls.clear()
            self.assertEqual(
                maestro._run_amend(
                    argparse.Namespace(run_id="run-1", plan="plan.json")
                ),
                0,
            )
            self.assertEqual(calls, [])
            self.assertEqual(
                maestro._run_status(argparse.Namespace(run_id="run-1")), 0
            )
            self.assertEqual(calls, [])

    def test_bind_existing_run_does_not_autoload(self) -> None:
        with mock.patch.object(maestro, "maybe_autoload_dashboard") as load:
            # _bind_existing_run is not invoked here; the resume test covers
            # that autoload sits outside it. Direct attribute check:
            import inspect

            source = inspect.getsource(maestro._bind_existing_run)
            self.assertNotIn("maybe_autoload_dashboard", source)
            load.assert_not_called()


@unittest.skipUnless(_INSTALL_PY.is_file(), _INSTALL_SKIP)
class InstallStampTest(unittest.TestCase):
    def test_apply_dashboard_stanza_writes_absolute_launcher(self) -> None:
        install = _load_install()
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "sssf"
            launcher = skill / "apps" / "dashboard" / "bin" / "maestro-dashboard"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            config = Path(tmp) / "maestro.config.yaml"
            config.write_text(
                "schema: maestro-config.v1\nruntime_state_root: /state\n",
                encoding="utf-8",
            )
            install.apply_dashboard_stanza(config, launcher)
            text = config.read_text(encoding="utf-8")
            self.assertIn("dashboard:", text)
            self.assertIn("launcher: {0}".format(launcher.resolve()), text)
            install.apply_dashboard_stanza(config, launcher)
            self.assertEqual(text.count("dashboard:"), 1)

    def test_launcher_path_is_skill_relative_not_checkout_literal(self) -> None:
        install = _load_install()
        skill = Path("/opt/sssf")
        got = install.dashboard_launcher_from_skill(skill)
        self.assertEqual(
            got, Path("/opt/sssf/apps/dashboard/bin/maestro-dashboard")
        )


if __name__ == "__main__":
    unittest.main()
