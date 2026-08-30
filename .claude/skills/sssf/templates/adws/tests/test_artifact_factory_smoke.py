"""Harness mechanics for the two-lane artifact-factory smoke. No live LLM."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
SMOKE_PATH = ADWS / "adw_modules" / "tools" / "artifact_factory_smoke.py"
FIXTURE = ADWS / "tests" / "fixtures" / "artifact_factory_smoke"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("artifact_factory_smoke", SMOKE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(str(SMOKE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


class ArtifactFactorySmokeHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_state_is_mode_0700_and_outside_product(self) -> None:
        product = smoke.init_product(self.root / "product")
        state = smoke.init_state(self.root / "state")
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertFalse(str(state).startswith(str(product)))
        self.assertEqual(product.parent, state.parent)

    def test_load_runtime_sync_registers_module_for_dataclasses(self) -> None:
        module = smoke.load_runtime_sync(ADWS / "tools" / "runtime_sync.py")
        self.assertIs(sys.modules[module.__name__], module)
        self.assertEqual(module.RuntimeCopy.__module__, module.__name__)
        self.assertIsNotNone(module.RuntimeCopy.__module__)
        broken = self.root / "broken_runtime_sync.py"
        broken.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            smoke.load_runtime_sync(broken)
        leftover = [
            name
            for name in sys.modules
            if name.startswith("_maestro_smoke_runtime_sync_")
            and getattr(sys.modules[name], "__file__", None) == str(broken.resolve())
        ]
        self.assertEqual(leftover, [])

    def test_mirror_installs_deployment_maestro_and_classifies(self) -> None:
        product = smoke.init_product(self.root / "product")
        info = smoke.install_adws(
            ADWS / "tools" / "runtime_sync.py",
            ADWS,
            product / "adws",
        )
        self.assertTrue((product / "adws" / "maestro.py").is_file())
        self.assertIn("maestro.py", " ".join(info["copied"] + info["unchanged"]))
        self.assertEqual(smoke.require_deployment_layout(product), "deployment")

    def test_deployed_cli_does_not_write_bytecode_into_product(self) -> None:
        product = smoke.init_product(self.root / "product")
        smoke.install_adws(
            ADWS / "tools" / "runtime_sync.py",
            ADWS,
            product / "adws",
        )
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        result = subprocess.run(
            [sys.executable, str(product / "adws" / "maestro.py"), "--help"],
            cwd=product,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(product.rglob("__pycache__")), [])

    def test_declared_commit_uses_smoke_identity_and_only_named_paths(self) -> None:
        product = smoke.init_product(self.root / "product")
        state = smoke.init_state(self.root / "state")
        smoke.install_adws(ADWS / "tools" / "runtime_sync.py", ADWS, product / "adws")
        config = smoke.write_deployment_config(
            product,
            state,
            executables={"herdr": "herdr", "omp": "omp", "claude": "claude"},
            runner_profile="grok-maestro",
            route_receipts={"omp": str(state / "receipts" / "omp.json")},
            route_verify_keys=(str(state / "keys" / "route.pub"),),
        )
        plan, seed = smoke.write_public_fixtures(product, FIXTURE)
        sha = smoke.commit_declared(product, (config, plan, seed))
        self.assertTrue(sha)
        files = smoke._git(product, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        self.assertEqual(
            sorted(files),
            [
                ".maestro/plans/two-lane.v1.json",
                "adws/maestro.config.yaml",
                "public/seed.txt",
            ],
        )
        identity = smoke._git(product, "log", "-1", "--format=%an <%ae>")
        self.assertEqual(
            identity,
            "{0} <{1}>".format(smoke.SMOKE_NAME, smoke.SMOKE_EMAIL),
        )
        loaded = json.loads(config.read_text(encoding="utf-8"))
        self.assertTrue(Path(loaded["runtime_state_root"]).is_absolute())
        self.assertEqual(loaded["runtime_state_root"], str(state))
        self.assertEqual(
            loaded["role_routes"]["builder"],
            {"route": "omp", "profile": "grok-maestro"},
        )
        self.assertEqual(
            loaded["route_receipts"]["omp"], str(state / "receipts" / "omp.json")
        )

    def test_private_leak_scans_only_objects_added_after_factory_baseline(self) -> None:
        product = smoke.init_product(self.root / "product")
        runtime = product / "runtime.py"
        runtime.write_text("selector = contract_field\n", encoding="utf-8")
        baseline = smoke.commit_declared(product, (runtime,))

        public = product / "public" / "a.txt"
        public.parent.mkdir()
        public.write_text("safe\n", encoding="utf-8")
        smoke.commit_declared(product, (public,))
        self.assertEqual(smoke.private_leak(product, baseline), [])

        leaked = product / "public" / "leak.txt"
        leaked.write_text("selector\n", encoding="utf-8")
        smoke.commit_declared(product, (leaked,))
        self.assertIn("public/leak.txt", smoke.private_leak(product, baseline))

    def test_relative_state_is_refused(self) -> None:
        product = smoke.init_product(self.root / "product")
        with self.assertRaises(smoke.SmokeRefused) as raised:
            smoke.write_deployment_config(
                product,
                Path("state"),
                executables={"herdr": "herdr"},
                runner_profile="grok-maestro",
                route_receipts={"omp": "/tmp/omp.json"},
                route_verify_keys=("/tmp/route.pub",),
            )
        self.assertEqual(raised.exception.code, "RUNTIME_STATE_NOT_ABSOLUTE")

    def test_missing_omp_fails_closed(self) -> None:
        with mock.patch.object(smoke.shutil, "which", return_value=None):
            with self.assertRaises(smoke.SmokeRefused) as raised:
                smoke.require_real_omp_route("grok-maestro")
        self.assertEqual(raised.exception.code, "DEPENDENCY_MISSING")

    def test_wrong_profile_fails_closed(self) -> None:
        with self.assertRaises(smoke.SmokeRefused) as raised:
            smoke.require_real_omp_route("not-the-route")
        self.assertEqual(raised.exception.code, "RUNNER_PROFILE")

    def test_parser_matches_plan_command(self) -> None:
        parser = smoke.build_parser()
        args = parser.parse_args(
            [
                "--runtime-sync",
                str(ADWS / "tools" / "runtime_sync.py"),
                "--template-adws",
                str(ADWS),
                "--fixture",
                str(FIXTURE),
                "--product-root",
                str(self.root / "product"),
                "--runner-profile",
                "grok-maestro",
            ]
        )
        self.assertEqual(args.runner_profile, "grok-maestro")
        self.assertFalse(hasattr(args, "route_receipts"))
        self.assertFalse(hasattr(args, "route_verify_key"))
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--runtime-sync",
                    str(ADWS / "tools" / "runtime_sync.py"),
                    "--template-adws",
                    str(ADWS),
                    "--fixture",
                    str(FIXTURE),
                    "--product-root",
                    str(self.root / "product"),
                    "--route-receipts",
                    "forged.json",
                ]
            )

    def test_admit_omp_route_uses_capture_spec(self) -> None:
        product = smoke.init_product(self.root / "product")
        state = smoke.init_state(self.root / "state")
        receipt = state / "receipts" / "omp.json"
        pubkey = state / "keys" / "route.pub"
        keys = mock.Mock()
        keys.route_seed = b"\x00" * 32
        keys.keys_dir = state / "keys"
        keys.keys_dir.mkdir(mode=0o700)
        pubkey.write_text("ab" * 32, encoding="ascii")
        written = mock.Mock(route="omp", path=receipt, reused=False)
        receipt.parent.mkdir(mode=0o700)
        receipt.write_text("{}\n", encoding="utf-8")

        def fake_admit(specs, destinations, *, route_seed):
            self.assertEqual(route_seed, keys.route_seed)
            spec = specs[0]
            self.assertEqual(spec.route, "omp")
            self.assertEqual(spec.profile, "grok-maestro")
            self.assertEqual(spec.model, "xai-oauth/grok-4.6")
            self.assertEqual(spec.effort, "high")
            self.assertEqual(spec.cwd, product)
            self.assertEqual(spec.herdr, Path("/bin/herdr"))
            self.assertEqual(spec.binary, Path("/bin/omp"))
            self.assertEqual(destinations["omp"], state / "receipts" / "omp.json")
            return (written,)

        with mock.patch.object(smoke.admission, "provision_keys", return_value=keys):
            with mock.patch.object(
                smoke.admission, "admit_routes", side_effect=fake_admit
            ):
                got_receipt, got_pub = smoke.admit_omp_route(
                    state,
                    product,
                    "grok-maestro",
                    Path("/bin/omp"),
                    Path("/bin/herdr"),
                )
        self.assertEqual(got_receipt, receipt)
        self.assertEqual(got_pub, pubkey)


if __name__ == "__main__":
    unittest.main()
